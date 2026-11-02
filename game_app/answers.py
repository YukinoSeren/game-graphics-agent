"""Answer normalisation, waiting-task continuation and amendment facts (01 §5.3).

A submitted answer becomes a new immutable ``InformationResponse`` plus a new
request projection and, where a device field changed, a new ``DeviceSnapshot``
version.  It never rewrites an old response or an already answered field's prior
evidence.  A ``facts_only`` amendment never touches the source task at all.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID, uuid5

from pydantic import ValidationError

from game_app.contracts.common import (
    AcquisitionAttempt,
    EvidenceRef,
    FieldResolution,
    FieldValue,
    MutationContext,
    Provenance,
    ResourceResult,
)
from game_app.contracts.device import DeviceSnapshot, DisplayContext, GpuSnapshot, Resolution
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    AnswerState,
    InformationContinuationMode,
    InformationRequestState,
    ValueState,
)
from game_app.contracts.information import (
    CreateInformationAmendmentRequest,
    InformationAmendment,
    InformationRequest,
    InformationResponse,
    InformationResponseInput,
    RequestedField,
)
from game_app.contracts.primitives import (
    InformationRequestRevisionRef,
    ObjectRef,
    Scalar,
    VersionRef,
    new_id,
    utc_now,
)
from game_app.errors import ContractError, ErrorCode, FieldError, empty_details
from game_app.files import FileService, PublishHooks
from game_app.repository import ProductRepository, UnitOfWork, canonical_json
from game_app.tasks import TaskService

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
ANSWER_ROUTE = "/api/information-responses"
AMENDMENT_ROUTE = "/api/information-amendments"
ANSWER_RULE_VERSION = "information-answer/0.4"

_GPU_PATH = re.compile(r"^gpus\.(\d+)\.(raw_name|vram_bytes|driver_version)$")


def _normalize_text(value: Scalar) -> Scalar | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _normalize_positive_int(value: Scalar) -> Scalar | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _normalize_positive_float(value: Scalar) -> Scalar | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


# Adding a normalisable field is one registry entry.  An unregistered path is
# never guessed: it stays unresolved.
_FIELD_NORMALIZERS: dict[str, Callable[[Scalar], Scalar | None]] = {
    "cpu": _normalize_text,
    "os_version": _normalize_text,
    "device_form": _normalize_text,
    "memory_bytes": _normalize_positive_int,
    "display.desktop_resolution.width": _normalize_positive_int,
    "display.desktop_resolution.height": _normalize_positive_int,
    "display.refresh_hz": _normalize_positive_float,
}
_GPU_NORMALIZERS: dict[str, Callable[[Scalar], Scalar | None]] = {
    "raw_name": _normalize_text,
    "driver_version": _normalize_text,
    "vram_bytes": _normalize_positive_int,
}


@dataclass(frozen=True)
class AnswerPlan:
    """The normalised answer set; nothing here is written on its own."""

    request: InformationRequest
    fields: list[RequestedField]
    state: InformationRequestState
    answers: dict[str, Scalar]
    unresolved: set[str]
    skipped: set[str]
    selected: dict[str, int]


def _coerce(model: Any, value: Any) -> Any:
    """Validate external input from its JSON form so enum values stay strict."""
    if isinstance(value, model):
        return value
    return model.model_validate_json(canonical_json(value))


class AnswerService:
    def __init__(
        self,
        repository: ProductRepository,
        files: FileService | None = None,
        *,
        tasks: TaskService | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = repository
        self.files = files
        self.tasks = tasks
        self.clock = clock

    # -- pure validation ----------------------------------------------------
    def validate_information_answer(
        self,
        response_input: InformationResponseInput,
        request: InformationRequest,
        *,
        response_id: str,
        now: str | None = None,
        prior_ref: ObjectRef | None = None,
    ) -> AnswerPlan:
        """Normalise answers against this request's field domain and type rules."""
        now = now or self.clock()
        prior = prior_ref or ObjectRef(
            entity_type="InformationRequest", entity_id=request.id
        )
        paths = {field.field_path for field in request.fields}
        unknown = (set(response_input.answers) | set(response_input.skipped_fields)) - paths
        if unknown:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the answer names fields this request did not ask about",
                field_errors=[
                    FieldError(path="answers", code="enum", message=f"unknown field {path}")
                    for path in sorted(unknown)
                ],
            )

        skipped = set(response_input.skipped_fields)
        fields: list[RequestedField] = []
        unresolved: set[str] = set()
        selected: dict[str, int] = {}
        for field in request.fields:
            path = field.field_path
            if path in skipped:
                fields.append(field.model_copy(update={
                    "answer_state": AnswerState.skipped,
                    "value": _skipped_value(field, response_id, now),
                }))
                continue
            if path not in response_input.answers:
                fields.append(field)
                continue
            if path in response_input.conflict_selections:
                index = response_input.conflict_selections[path]
                value = _resolved_value(field, index, response_id, now, prior)
                selected[path] = index
                fields.append(field.model_copy(update={
                    "answer_state": AnswerState.answered,
                    "value": value,
                }))
                continue
            if field.value.value_state is ValueState.conflicting:
                # An answer alone does not pick between evidence-backed
                # candidates; the player must select one explicitly.
                unresolved.add(path)
                fields.append(field.model_copy(update={"answer_state": AnswerState.unresolved}))
                continue
            normalized = _normalize(path, response_input.answers[path])
            if normalized is None:
                unresolved.add(path)
                fields.append(field.model_copy(update={
                    "answer_state": AnswerState.unresolved,
                    "value": _unresolved_value(field, response_id, now),
                }))
                continue
            fields.append(field.model_copy(update={
                "answer_state": AnswerState.answered,
                "value": _answered_value(normalized, response_id, path, now),
            }))

        return AnswerPlan(
            request=request,
            fields=fields,
            state=_reduce_state(fields),
            answers=dict(response_input.answers),
            unresolved=unresolved,
            skipped=skipped,
            selected=selected,
        )

    # -- submission ---------------------------------------------------------
    def answer_information(
        self,
        response_input: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
    ) -> ResourceResult:
        response_input = _coerce(InformationResponseInput, response_input)
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": "POST",
            "route": ANSWER_ROUTE,
            "request": response_input.model_dump(mode="json"),
        })).hexdigest()
        with self.repository.unit_of_work() as uow:
            replay = self._replay(uow, context, request_schema_version, request_hash)
            if replay is not None:
                return replay

            request = self._load_request(uow, response_input.request_id)
            self._require_task_owner(uow, request.source_task_id, context.user_scope_id)
            if response_input.expected_request_version != request.version:
                raise ContractError(
                    ErrorCode.INFORMATION_REQUEST_VERSION_CONFLICT,
                    "the question changed; read it again before answering",
                    details=empty_details(
                        current_resource_ref=ObjectRef(
                            entity_type="InformationRequest", entity_id=request.id,
                        )
                    ),
                )
            if response_input.expected_context_revision != request.expected_context_revision:
                raise ContractError(
                    ErrorCode.INFORMATION_CONTEXT_CHANGED,
                    "the question's context moved on; read the current question",
                    details=empty_details(
                        current_resource_ref=ObjectRef(
                            entity_type="InformationRequest", entity_id=request.id,
                        )
                    ),
                )
            if request.state is InformationRequestState.superseded:
                raise ContractError(
                    ErrorCode.INFORMATION_REQUEST_STATE_CONFLICT,
                    "this question was superseded; read the replacement",
                    details=empty_details(
                        current_resource_ref=(
                            None if request.superseded_by_request_id is None
                            else ObjectRef(
                                entity_type="InformationRequest",
                                entity_id=request.superseded_by_request_id,
                            )
                        )
                    ),
                )
            if request.state in (
                InformationRequestState.resolved,
                InformationRequestState.skipped,
            ):
                # A handled question is answered through the facts_only
                # amendment entry; a direct answer would silently rewrite it.
                raise ContractError(
                    ErrorCode.INFORMATION_REQUEST_STATE_CONFLICT,
                    "this question is already handled; create a correction instead",
                    details=empty_details(
                        current_resource_ref=ObjectRef(
                            entity_type="InformationRequest", entity_id=request.id,
                        )
                    ),
                )

            now = self.clock()
            response_id = new_id()
            prior_ref = self._prior_ref(request)
            plan = self.validate_information_answer(
                response_input, request, response_id=response_id, now=now, prior_ref=prior_ref
            )
            # A response describes this submission only: fields the player did
            # not submit here belong to the request projection, not to it.
            submitted = set(response_input.answers)
            derived = self._derive_snapshot(
                uow, request, plan, response_id, now, submitted=submitted
            )
            answered_fields = [
                field.field_path for field in plan.fields
                if field.field_path in submitted
                and field.answer_state is AnswerState.answered
            ]
            unresolved_fields = [
                field.field_path for field in plan.fields
                if field.field_path in submitted
                and field.answer_state is AnswerState.unresolved
            ]
            answered_values = {
                field.field_path: field.value.value
                for field in plan.fields
                if field.field_path in submitted
                and field.answer_state is AnswerState.answered
            }

            result_refs: list[ObjectRef] = [
                ObjectRef(entity_type="InformationResponse", entity_id=response_id),
                ObjectRef(entity_type="InformationRequest", entity_id=request.id),
            ]
            if derived is not None:
                result_refs.append(ObjectRef(entity_type="DeviceSnapshot", entity_id=derived.id))
            response = InformationResponse(
                id=response_id,
                schema_version=PRODUCT_CONTRACT_VERSION,
                created_at=now,
                created_by_task_id=None,
                request_id=request.id,
                expected_request_version=response_input.expected_request_version,
                expected_context_revision=response_input.expected_context_revision,
                answers=dict(response_input.answers),
                skipped_fields=list(response_input.skipped_fields),
                raw_text=response_input.raw_text,
                conflict_selections=dict(response_input.conflict_selections),
                answered_fields=answered_fields,
                unresolved_fields=unresolved_fields,
                answered_values=answered_values,
                submitted_at=now,
                result_refs=[
                    ref for ref in result_refs
                    if ref.entity_type == "DeviceSnapshot"
                ],
                post_request_version=request.version + 1,
                post_context_revision=request.expected_context_revision + 1,
            )
            uow.insert_object("InformationResponse", response)
            if request.continuation_mode is InformationContinuationMode.source_task:
                # A partial answer may wake nothing, so the durable link is the
                # task_responses projection, not the input's cause_ref.
                sequence = int(uow.query(
                    "SELECT COALESCE(MAX(sequence), 0) AS last FROM task_responses"
                    " WHERE task_id=?",
                    (request.source_task_id,),
                )[0]["last"]) + 1
                uow.execute(
                    "INSERT INTO task_responses(task_id, sequence, response_id,"
                    " request_id, created_at) VALUES(?, ?, ?, ?, ?)",
                    (request.source_task_id, sequence, response_id, request.id, now),
                )
            if derived is not None:
                uow.insert_versioned("DeviceSnapshot", derived)
                uow.insert_reference(response_id, "derived_snapshot", ObjectRef(
                    entity_type="DeviceSnapshot", entity_id=derived.id,
                ))
            updated = request.model_copy(update={
                "version": request.version + 1,
                "expected_context_revision": request.expected_context_revision + 1,
                "fields": plan.fields,
                "state": plan.state,
                "result_refs": list(request.result_refs) + result_refs,
            })
            self._write_request_version(uow, request, updated)

            if self._should_wake(plan, request):
                if self.tasks is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "no task service is configured to continue a waiting task",
                    )
                self.tasks.append_input_revision_and_wake(
                    uow,
                    request.source_task_id,
                    cause_ref=ObjectRef(
                        entity_type="InformationResponse", entity_id=response_id,
                    ),
                    related=[
                        ObjectRef(entity_type="InformationRequest", entity_id=request.id),
                        ObjectRef(entity_type="InformationResponse", entity_id=response_id),
                    ],
                )

            result = ResourceResult(refs=result_refs)
            uow.insert_idempotency(
                user_scope_id=context.user_scope_id,
                method="POST",
                canonical_route=ANSWER_ROUTE,
                key=context.idempotency_key,
                request_schema_version=request_schema_version,
                request_hash=request_hash,
                response_status=201,
                response_body=canonical_json(result.model_dump(mode="json")),
                result_refs=[ref.model_dump(mode="json") for ref in result.refs],
            )
            return result

    # -- task continuation --------------------------------------------------
    @staticmethod
    def _should_wake(plan: AnswerPlan, request: InformationRequest) -> bool:
        if request.continuation_mode is not InformationContinuationMode.source_task:
            return False
        critical = [
            field.field_path for field in plan.fields if field.required_for
        ]
        if not critical:
            return False
        states = {field.field_path: field.answer_state for field in plan.fields}
        return all(
            states[path] in (AnswerState.answered, AnswerState.skipped)
            for path in critical
        )

    # -- storage helpers ----------------------------------------------------
    def _replay(
        self,
        uow: UnitOfWork,
        context: MutationContext,
        request_schema_version: str,
        request_hash: str,
    ) -> ResourceResult | None:
        existing = uow.find_idempotency(
            context.user_scope_id, "POST", ANSWER_ROUTE, context.idempotency_key
        )
        if existing is None:
            return None
        if existing.lifecycle.value == "tombstoned":
            raise ContractError(ErrorCode.RESOURCE_GONE, "this answer was cleaned up")
        if (
            existing.request_schema_version != request_schema_version
            or existing.request_hash != request_hash
        ):
            raise ContractError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "the same key was already used with different answer content",
                field_errors=[FieldError(
                    path="idempotency_key", code="conflict", message="different request hash",
                )],
            )
        return ResourceResult.model_validate(json.loads(existing.response_body))
    def _load_request(self, uow: UnitOfWork, request_id: str) -> InformationRequest:
        head = uow.get_head("InformationRequest", request_id)
        try:
            if head is None:
                stored = uow.find_object("InformationRequest", request_id)
                if stored is None:
                    raise ContractError(
                        ErrorCode.RESOURCE_NOT_FOUND, "the requested question is not available"
                    )
                return InformationRequest.model_validate_json(stored.payload_bytes)
            return InformationRequest.model_validate_json(
                canonical_json(uow.get_snapshot("InformationRequest", request_id, head))
            )
        except ValidationError as error:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "this question predates the fixed-root contract and cannot be extended",
            ) from error

    @staticmethod
    def _require_task_owner(
        uow: UnitOfWork, task_id: str, user_scope_id: str
    ) -> None:
        _require_task_owner(uow, task_id, user_scope_id)

    def _write_request_version(
        self,
        uow: UnitOfWork,
        previous: InformationRequest,
        updated: InformationRequest,
    ) -> None:
        head = uow.get_head("InformationRequest", previous.id)
        if head is None:
            uow.append_snapshot("InformationRequest", previous.id, previous)
            head = 1
        if head != previous.version:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the question projection is not at the version this answer read",
            )
        if updated.version != head + 1:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR, "an answer must add exactly one request version"
            )
        uow.append_snapshot("InformationRequest", previous.id, updated)

    def _prior_ref(self, request: InformationRequest) -> ObjectRef:
        snapshot = next(
            (ref for ref in request.context_refs if ref.entity_type == "DeviceSnapshot"),
            None,
        )
        if snapshot is not None:
            return ObjectRef(entity_type="DeviceSnapshot", entity_id=snapshot.entity_id)
        return ObjectRef(entity_type="InformationRequest", entity_id=request.id)

    # -- derived snapshot ---------------------------------------------------
    def _derive_snapshot(
        self,
        uow: UnitOfWork,
        request: InformationRequest,
        plan: AnswerPlan,
        response_id: str,
        now: str,
        *,
        submitted: set[str],
    ) -> DeviceSnapshot | None:
        ref = next(
            (item for item in request.context_refs if item.entity_type == "DeviceSnapshot"),
            None,
        )
        if ref is None:
            return None
        # The answer chains from the newest version of the same device series:
        # a partially answered request may be answered again, and each answer
        # must add exactly one version instead of colliding with the last one.
        stored = uow.latest_for_series("DeviceSnapshot", ref.series_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the question's device snapshot no longer exists",
            )
        old = DeviceSnapshot.model_validate_json(stored.payload_bytes)
        owner = uow.query(
            "SELECT user_scope_id FROM tasks WHERE task_id=?",
            (request.source_task_id,),
        )
        if not owner or old.user_scope_id != owner[0]["user_scope_id"]:
            # The question's series must belong to the task owner; a foreign
            # series is never merged into this user's facts.
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the device snapshot belongs to another user",
            )
        values = {
            field.field_path: field.value
            for field in plan.fields
            if field.field_path in submitted
            and field.answer_state in (AnswerState.answered, AnswerState.unresolved)
        }
        if not values:
            return None
        return _apply_device_values(old, values, now)


class AmendmentService:
    """Create a ``facts_only`` successor question for a handled request."""

    def __init__(
        self,
        repository: ProductRepository,
        files: FileService,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = repository
        self.files = files
        self.clock = clock

    def create_information_amendment(
        self,
        request: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        request = _coerce(CreateInformationAmendmentRequest, request)
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": "POST",
            "route": AMENDMENT_ROUTE,
            "request": request.model_dump(mode="json"),
        })).hexdigest()
        ids = {
            role: _deterministic_id(
                context.user_scope_id, AMENDMENT_ROUTE, context.idempotency_key,
                request_hash, role,
            )
            for role in ("amendment", "request")
        }
        if not self._has_registered(context):
            with self.repository.unit_of_work() as uow:
                source = self._load_request(uow, request.source_request_id)
                _require_task_owner(uow, source.source_task_id, context.user_scope_id)
                self._check_source(uow, source, request)
                root, _root_version = self._root(uow, source)
                self._selected_fields(root, request.field_paths)
        manifest = {
            "kind": "information_amendment",
            "rule_version": ANSWER_RULE_VERSION,
            "input": request.model_dump(mode="json"),
            "new_request_id": ids["request"],
            "reason": request.reason,
        }
        published = self.files.publish(
            kind="import",
            files={
                f"amendments/{ids['amendment']}/declaration.json":
                    canonical_json(manifest)
            },
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=AMENDMENT_ROUTE,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write(
                uow,
                request=request,
                ids=ids,
                managed=managed,
                user_scope_id=context.user_scope_id,
            ),
            hooks=hooks,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _write(
        self,
        uow: UnitOfWork,
        *,
        request: CreateInformationAmendmentRequest,
        ids: Mapping[str, str],
        managed: Sequence[Any],
        user_scope_id: str,
    ) -> list[ObjectRef]:
        source = self._load_request(uow, request.source_request_id)
        _require_task_owner(uow, source.source_task_id, user_scope_id)
        self._check_source(uow, source, request)
        root, root_version = self._root(uow, source)
        fields = self._selected_fields(root, request.field_paths)
        now = self.clock()
        successor = InformationRequest(
            id=ids["request"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            source_task_id=source.source_task_id,
            version=1,
            context_refs=list(source.context_refs),
            expected_context_revision=1,
            fields=fields,
            state=InformationRequestState.open,
            result_refs=[],
            superseded_by_request_id=None,
            continuation_mode=InformationContinuationMode.facts_only,
            amends_request_id=source.id,
            amendment_root_ref=InformationRequestRevisionRef(
                request_id=root.id, version=root_version
            ),
        )
        amendment = InformationAmendment(
            id=ids["amendment"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            input=request,
            new_request_id=successor.id,
            declaration_file_id=managed[0].id,
        )
        uow.insert_object("InformationRequest", successor)
        uow.append_snapshot("InformationRequest", successor.id, successor)
        uow.insert_object("InformationAmendment", amendment)
        uow.record_information_amendment(
            successor_id=successor.id,
            source_request_id=source.id,
            root_request_id=root.id,
            root_version=root_version,
        )
        amendment_ref = ObjectRef(entity_type="InformationAmendment", entity_id=amendment.id)
        request_ref = ObjectRef(entity_type="InformationRequest", entity_id=successor.id)
        source_ref = ObjectRef(entity_type="InformationRequest", entity_id=source.id)
        uow.insert_reference(amendment.id, "request", request_ref)
        uow.insert_reference(amendment.id, "source_request", source_ref)
        uow.insert_reference(successor.id, "amends", source_ref)
        return [
            amendment_ref,
            request_ref,
            ObjectRef(entity_type="ManagedFile", entity_id=managed[0].id),
        ]

    # -- guards -------------------------------------------------------------
    def _check_source(
        self,
        uow: UnitOfWork,
        source: InformationRequest,
        request: CreateInformationAmendmentRequest,
    ) -> None:
        if request.expected_request_version != source.version:
            raise ContractError(
                ErrorCode.INFORMATION_REQUEST_VERSION_CONFLICT,
                "the source question changed; read it again",
                details=empty_details(
                    current_resource_ref=ObjectRef(
                        entity_type="InformationRequest", entity_id=source.id,
                    )
                ),
            )
        if request.expected_context_revision != source.expected_context_revision:
            raise ContractError(
                ErrorCode.INFORMATION_CONTEXT_CHANGED,
                "the source question's context revision is not the one submitted",
                details=empty_details(
                    current_resource_ref=ObjectRef(
                        entity_type="InformationRequest", entity_id=source.id,
                    )
                ),
            )
        if source.state is InformationRequestState.superseded:
            raise ContractError(
                ErrorCode.INFORMATION_CONTEXT_CHANGED,
                "this question was superseded; continue from its replacement",
                details=empty_details(
                    current_resource_ref=(
                        None if source.superseded_by_request_id is None
                        else ObjectRef(
                            entity_type="InformationRequest",
                            entity_id=source.superseded_by_request_id,
                        )
                    )
                ),
            )
        if source.state in (
            InformationRequestState.open,
            InformationRequestState.partially_answered,
        ):
            raise ContractError(
                ErrorCode.INFORMATION_REQUEST_STATE_CONFLICT,
                "answer the original question before creating a correction",
                details=empty_details(
                    current_resource_ref=ObjectRef(
                        entity_type="InformationRequest", entity_id=source.id,
                    )
                ),
            )
        successor = uow.query(
            "SELECT successor_id FROM information_amendment_links WHERE source_request_id=?",
            (source.id,),
        )
        if successor:
            raise ContractError(
                ErrorCode.INFORMATION_CONTEXT_CHANGED,
                "this question already has a direct correction; continue from it",
                details=empty_details(
                    replacement_resource_ref=ObjectRef(
                        entity_type="InformationRequest",
                        entity_id=successor[0]["successor_id"],
                    )
                ),
            )

    def _root(
        self, uow: UnitOfWork, source: InformationRequest
    ) -> tuple[InformationRequest, int]:
        if source.amendment_root_ref is not None:
            root_id = source.amendment_root_ref.request_id
            root_version = source.amendment_root_ref.version
        else:
            root_id, root_version = source.id, source.version
        try:
            head = uow.get_head("InformationRequest", root_id)
            if head is None:
                stored = uow.find_object("InformationRequest", root_id)
                if stored is None:
                    raise ContractError(
                        ErrorCode.RESOURCE_NOT_FOUND, "the amendment root does not exist"
                    )
                model = InformationRequest.model_validate_json(stored.payload_bytes)
            else:
                model = InformationRequest.model_validate_json(
                    canonical_json(uow.get_snapshot("InformationRequest", root_id, root_version))
                )
        except (ValidationError, ContractError) as error:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the amendment root snapshot is missing or unreadable; no root was guessed",
            ) from error
        if model.version != root_version:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the amendment root snapshot is not the fixed version it names",
            )
        return model, root_version

    @staticmethod
    def _selected_fields(
        root: InformationRequest, field_paths: Sequence[str]
    ) -> list[RequestedField]:
        by_path = {field.field_path: field for field in root.fields}
        unknown = [path for path in field_paths if path not in by_path]
        if unknown:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a correction may only ask fields from the root question",
                field_errors=[
                    FieldError(path="field_paths", code="enum", message=f"not in root: {path}")
                    for path in unknown
                ],
            )
        return [
            RequestedField(
                field_path=by_path[path].field_path,
                purpose=by_path[path].purpose,
                required_for=list(by_path[path].required_for),
                candidates=list(by_path[path].candidates),
                answer_state=AnswerState.unanswered,
                value=_unknown_field(),
            )
            for path in field_paths
        ]

    def _has_registered(self, context: MutationContext) -> bool:
        return self.repository.find_idempotency(
            context.user_scope_id, "POST", AMENDMENT_ROUTE, context.idempotency_key
        ) is not None

    def _load_request(self, uow: UnitOfWork, request_id: str) -> InformationRequest:
        head = uow.get_head("InformationRequest", request_id)
        try:
            if head is None:
                stored = uow.find_object("InformationRequest", request_id)
                if stored is None:
                    raise ContractError(
                        ErrorCode.RESOURCE_NOT_FOUND, "the requested question is not available"
                    )
                return InformationRequest.model_validate_json(stored.payload_bytes)
            return InformationRequest.model_validate_json(
                canonical_json(uow.get_snapshot("InformationRequest", request_id, head))
            )
        except ValidationError as error:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "this question predates the fixed-root contract and cannot be extended",
            ) from error


# -- pure helpers -------------------------------------------------------------


def _require_task_owner(
    uow: UnitOfWork, task_id: str, user_scope_id: str
) -> None:
    """Only the owner of the asking task may read or extend its question.

    Another user's question is indistinguishable from a missing one, so neither
    its existence nor its content leaks.  This also protects the wake path: a
    stranger can never change someone else's task input, state or events.
    """
    rows = uow.query("SELECT user_scope_id FROM tasks WHERE task_id=?", (task_id,))
    if not rows or rows[0]["user_scope_id"] != user_scope_id:
        raise ContractError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "the requested question is not available",
        )


_ID_NAMESPACE = UUID("9c1d2e3f-4a5b-4c6d-8e7f-0a1b2c3d4e5f")


def _deterministic_id(*parts: str) -> str:
    return uuid5(_ID_NAMESPACE, ":".join(parts)).hex


def _unknown_field() -> FieldValue:
    return FieldValue(
        value_state=ValueState.unknown,
        value=None,
        unknown_reason="not_provided",
        acquisition_status=AcquisitionStatus.not_attempted,
    )


def _normalize(path: str, value: Scalar) -> Scalar | None:
    """Return the typed value, or None when the answer cannot be interpreted."""
    normalizer = _FIELD_NORMALIZERS.get(path)
    if normalizer is None:
        gpu = _GPU_PATH.match(path)
        if gpu is not None:
            normalizer = _GPU_NORMALIZERS.get(gpu.group(2))
    if normalizer is None:
        return None
    return normalizer(value)


def _local_evidence(response_id: str, path: str) -> EvidenceRef:
    return EvidenceRef(
        kind="local",
        entity_type="InformationResponse",
        entity_id=response_id,
        field_path=path,
    )


def _attempt(
    response_id: str,
    now: str,
    status: AcquisitionStatus,
    *,
    method: AcquisitionMethod = AcquisitionMethod.user_provided,
    error_code: str | None = None,
) -> AcquisitionAttempt:
    return AcquisitionAttempt(
        id=new_id(),
        method=method,
        status=status,
        attempted_at=now,
        error_code=error_code,
        information_response_id=response_id,
    )


def _answered_value(value: Scalar, response_id: str, path: str, now: str) -> FieldValue:
    """The answer itself; the snapshot merge appends it to the probe history."""
    return FieldValue[Scalar](
        value_state=ValueState.known,
        value=value,
        acquisition_status=AcquisitionStatus.succeeded,
        provenance=[Provenance(
            method=AcquisitionMethod.user_provided,
            observed_at=None,
            recorded_at=now,
            evidence_refs=[_local_evidence(response_id, path)],
            note="player answer",
        )],
        attempts=[_attempt(response_id, now, AcquisitionStatus.succeeded)],
    )


def _merge_history(new_value: FieldValue, previous: FieldValue | None) -> FieldValue:
    """Append a new answer to what the snapshot already recorded.

    A failed probe, its provenance and its attempts stay in the record; the
    answer is one more attempt, not a replacement of the acquisition history.
    """
    if previous is None:
        return new_value
    return new_value.model_copy(update={
        "provenance": [*previous.provenance, *new_value.provenance],
        "attempts": [*previous.attempts, *new_value.attempts],
    })


def _skipped_value(field: RequestedField, response_id: str, now: str) -> FieldValue:
    prior = field.value
    return FieldValue[Scalar](
        value_state=ValueState.unknown,
        value=None,
        unknown_reason=prior.unknown_reason or "not_provided",
        candidates=list(prior.candidates),
        provenance=list(prior.provenance),
        acquisition_status=AcquisitionStatus.skipped,
        attempts=[_attempt(response_id, now, AcquisitionStatus.skipped)],
    )


def _unresolved_value(field: RequestedField, response_id: str, now: str) -> FieldValue:
    prior = field.value
    return FieldValue[Scalar](
        value_state=ValueState.unknown,
        value=None,
        unknown_reason="parse_failed",
        candidates=list(prior.candidates),
        provenance=list(prior.provenance),
        acquisition_status=AcquisitionStatus.failed,
        attempts=[_attempt(
            response_id, now, AcquisitionStatus.failed, error_code="ANSWER_UNRESOLVED",
        )],
    )


def _resolved_value(
    field: RequestedField, index: int, response_id: str, now: str, prior_ref: ObjectRef
) -> FieldValue:
    prior = field.value
    if prior.value_state is not ValueState.conflicting:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            "only a conflicting value can be resolved by selecting a candidate",
            field_errors=[FieldError(
                path=f"conflict_selections.{field.field_path}", code="conflict",
                message="the field is not conflicting",
            )],
        )
    if not 0 <= index < len(prior.candidates):
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            "the selected candidate index does not exist",
            field_errors=[FieldError(
                path=f"conflict_selections.{field.field_path}", code="range",
                message=f"index {index} is out of range",
            )],
        )
    candidate = prior.candidates[index]
    provenance = list(candidate.provenance)
    provenance.append(Provenance(
        method=AcquisitionMethod.player_confirmed,
        observed_at=None,
        recorded_at=now,
        evidence_refs=[_local_evidence(response_id, field.field_path)],
        note=f"player selected candidate {index}",
    ))
    return FieldValue[Scalar](
        value_state=ValueState.known,
        value=candidate.value,
        candidates=list(prior.candidates),
        provenance=provenance,
        acquisition_status=AcquisitionStatus.succeeded,
        attempts=[
            *prior.attempts,
            _attempt(
                response_id, now, AcquisitionStatus.succeeded,
                method=AcquisitionMethod.player_confirmed,
            ),
        ],
        resolution=FieldResolution(
            selected_provenance_indexes=[0],
            reason=f"player selected candidate {index}",
            prior_object_ref=prior_ref,
        ),
    )


def _reduce_state(fields: Sequence[RequestedField]) -> InformationRequestState:
    states = [field.answer_state for field in fields]
    if all(state is AnswerState.unanswered for state in states):
        return InformationRequestState.open
    remaining = [
        state for state in states
        if state in (AnswerState.unanswered, AnswerState.unresolved)
    ]
    if remaining:
        return InformationRequestState.partially_answered
    skipped = [state for state in states if state is AnswerState.skipped]
    if skipped:
        return InformationRequestState.skipped
    return InformationRequestState.resolved


def _apply_device_values(
    old: DeviceSnapshot, answers: Mapping[str, FieldValue], now: str
) -> DeviceSnapshot:
    # Adapters are addressed by their probe index, never by list position: the
    # stored list only holds members with a known name and may be sparse.
    gpus = {gpu.probe_index: gpu.model_copy() for gpu in old.gpus}
    updates: dict[str, Any] = {}
    display = old.display
    axes = dict(display.desktop_resolution_axes)

    ordered = sorted(
        answers.items(),
        # A newly named adapter carries its name first, so the other attributes
        # have a member to attach to in the same answer set.
        key=lambda item: (not item[0].endswith(".raw_name"), item[0]),
    )
    for path, value in ordered:
        if path in ("cpu", "memory_bytes", "os_version", "device_form"):
            updates[path] = _merge_history(value, getattr(old, path))
            continue
        if path == "display.refresh_hz":
            display = DisplayContext(
                desktop_resolution=display.desktop_resolution,
                desktop_resolution_axes=axes,
                refresh_hz=_merge_history(value, display.refresh_hz),
            )
            continue
        if path in (
            "display.desktop_resolution.width",
            "display.desktop_resolution.height",
        ):
            axis = path.rsplit(".", 1)[1]
            axes[axis] = _merge_history(value, axes.get(axis))
            continue
        match = _GPU_PATH.match(path)
        if match is None:
            continue
        index, attribute = int(match.group(1)), match.group(2)
        existing = gpus.get(index)
        if existing is None:
            if attribute != "raw_name" or value.value_state is not ValueState.known:
                # Without a known name there is no adapter member to attach the
                # other attributes to; an unresolved name does not create one.
                continue
            gpus[index] = GpuSnapshot(
                id=new_id(),
                probe_index=index,
                raw_name=value,
                vram_bytes=_unknown_field(),
                driver_version=_unknown_field(),
            )
        else:
            gpus[index] = existing.model_copy(update={
                attribute: _merge_history(value, getattr(existing, attribute)),
            })

    if {"width", "height"} <= set(axes) and any(
        path in (
            "display.desktop_resolution.width",
            "display.desktop_resolution.height",
        )
        for path in answers
    ):
        width, height = axes["width"], axes["height"]
        if (
            width.value_state is ValueState.known
            and height.value_state is ValueState.known
            and width.value is not None
            and height.value is not None
        ):
            display = DisplayContext(
                desktop_resolution=FieldValue[Resolution](
                    value_state=ValueState.known,
                    value=Resolution(width=width.value, height=height.value),
                    acquisition_status=AcquisitionStatus.succeeded,
                    provenance=[*width.provenance, *height.provenance],
                    attempts=[*width.attempts, *height.attempts],
                ),
                desktop_resolution_axes=axes,
                refresh_hz=display.refresh_hz,
            )
        else:
            display = DisplayContext(
                desktop_resolution=display.desktop_resolution,
                desktop_resolution_axes=axes,
                refresh_hz=display.refresh_hz,
            )

    return old.model_copy(update={
        **updates,
        "id": new_id(),
        "version": old.version + 1,
        "created_at": now,
        "captured_at": now,
        "gpus": [gpus[index] for index in sorted(gpus)],
        "display": display,
    })


__all__ = [
    "AMENDMENT_ROUTE",
    "ANSWER_ROUTE",
    "ANSWER_RULE_VERSION",
    "AmendmentService",
    "AnswerPlan",
    "AnswerService",
]
