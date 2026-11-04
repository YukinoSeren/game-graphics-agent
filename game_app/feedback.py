"""05 feedback: the player's words, explicit preferences and acceptance.

Three rules shape this module.  The original words are saved before any model
call and are never rewritten.  A model may produce a new, linked interpretation
of them, but it can never declare a preference or an acceptance.  A preference
version or an acceptance is compared against the current record inside the
write transaction, so a stale correction conflicts instead of overwriting.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Mapping, Sequence

from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    FieldValue,
    MutationContext,
    ObjectRef,
    Provenance,
    ResourceResult,
    TaskAccepted,
)
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    InterpretationStatus,
    PreferenceExplicitness,
    PreferenceLifecycle,
    PreferenceScope,
    ValueState,
)
from game_app.contracts.feedback import (
    Feedback,
    FeedbackInput,
    FeedbackInterpretationLink,
    Preference,
    PreferenceDeclaration,
    PreferenceDeclarationInput,
    Symptom,
    TargetAcceptance,
    TargetAcceptanceInput,
)
from game_app.contracts.performance import PlaySession, SessionSegment
from game_app.contracts.primitives import utc_now
from game_app.errors import ContractError, ErrorCode, FieldError, empty_details
from game_app.repository import UnitOfWork, canonical_json

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
FEEDBACK_ROUTE = "/api/feedback"
PREFERENCE_ROUTE = "/api/preferences"
TARGET_ACCEPTANCE_ROUTE = "/api/target-acceptances"
MODEL_PROMPT_VERSION = "feedback-parse/1"
MODEL_OUTPUT_SCHEMA_VERSION = "feedback-interpretation/1"
INTERPRETATION_RULE_VERSION = "feedback-interpretation-checks/0.4"

_ID_NAMESPACE = uuid.NAMESPACE_URL
# The vocabulary in 05 §2 is advisory: ``kind`` is contractually free text, so
# an unfamiliar word is kept rather than silently rewritten or dropped.


def _stable_id(*parts: Any) -> str:
    return uuid.uuid5(_ID_NAMESPACE, "feedback:" + ":".join(map(str, parts))).hex


def _original_feedback_id(typed: FeedbackInput) -> str:
    """Identical words in the same session are one fact, not two feedbacks."""
    return _stable_id(
        "feedback-original", typed.session_id, typed.segment_id or "",
        typed.raw_text, canonical_json(typed.selections).decode("utf-8"),
    )


def _derived_feedback_id(task_id: str) -> str:
    return _stable_id("feedback", task_id, "interpretation")


def _link_id(task_id: str) -> str:
    return _stable_id("feedback", task_id, "interpretation-link")


def _check(
    check_id: str, status: CheckStatus, *, now: str, note: str,
    evidence_refs: Sequence[EvidenceRef] = (),
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=INTERPRETATION_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=list(evidence_refs),
        checked_at=None if status is CheckStatus.not_run else now,
        method="deterministic feedback rule",
        note=note,
    )


def _unknown_field(reason: str = "not_provided") -> FieldValue[str]:
    return FieldValue(
        value_state=ValueState.unknown,
        value=None,
        unknown_reason=reason,
        acquisition_status=AcquisitionStatus.not_attempted,
    )


def _agent_field(
    value: str, *, original_id: str, when: str,
) -> FieldValue[str]:
    """A value read out of the player's own words is still only a reading."""
    evidence = EvidenceRef(kind="local", entity_type="Feedback", entity_id=original_id)
    return FieldValue(
        value_state=ValueState.known,
        value=value,
        acquisition_status=AcquisitionStatus.succeeded,
        provenance=[Provenance(
            method=AcquisitionMethod.agent_derived,
            observed_at=None,
            recorded_at=when,
            evidence_refs=[evidence],
        )],
    )


def _load_optional(
    repository: Any, entity_type: str, object_id: str, model: Any,
) -> Any | None:
    stored = repository.find_object(entity_type, object_id)
    if stored is None:
        return None
    return model.model_validate_json(stored.payload_bytes)


def _load_session(repository: Any, session_id: str) -> PlaySession | None:
    with repository.unit_of_work() as uow:
        head = uow.get_head("PlaySession", session_id)
        if head is None:
            stored = uow.find_object("PlaySession", session_id)
            if stored is None:
                return None
            return PlaySession.model_validate_json(stored.payload_bytes)
        return PlaySession.model_validate_json(
            canonical_json(uow.get_snapshot("PlaySession", session_id, head))
        )


def _load_segment(repository: Any, segment_id: str) -> SessionSegment | None:
    with repository.unit_of_work() as uow:
        head = uow.get_head("SessionSegment", segment_id)
        if head is None:
            stored = uow.find_object("SessionSegment", segment_id)
            if stored is None:
                return None
            return SessionSegment.model_validate_json(stored.payload_bytes)
        return SessionSegment.model_validate_json(
            canonical_json(uow.get_snapshot("SessionSegment", segment_id, head))
        )


def _session_owner(repository: Any, session: PlaySession) -> str | None:
    """The owner is the installation's scope, as in 04 (never inferred)."""
    with repository.unit_of_work() as uow:
        stored = uow.latest_for_series("GameInstallation", session.installation_id)
    if stored is None:
        return None
    return stored.payload.get("user_scope_id")


def _require_session(
    repository: Any, session_id: str, user_scope_id: str,
) -> PlaySession:
    session = _load_session(repository, session_id)
    if session is None or _session_owner(repository, session) != user_scope_id:
        raise ContractError(
            ErrorCode.RESOURCE_NOT_FOUND, "the requested session is not available"
        )
    return session


def _require_segment(
    repository: Any, segment_id: str, session_id: str,
) -> SessionSegment:
    segment = _load_segment(repository, segment_id)
    if segment is None or segment.session_id != session_id:
        raise ContractError(
            ErrorCode.REFERENCE_INVALID,
            "the segment does not belong to the named session",
            field_errors=[FieldError(
                path="segment_id", code="reference",
                message="segment and session disagree",
            )],
        )
    return segment


def _task_scope(repository: Any, task_id: str | None) -> str | None:
    if not task_id:
        return None
    with repository.unit_of_work() as uow:
        rows = uow.query("SELECT user_scope_id FROM tasks WHERE task_id=?", (task_id,))
    return None if not rows else rows[0]["user_scope_id"]


def _feedback_owner(repository: Any, feedback: Feedback) -> str | None:
    owner = _task_scope(repository, feedback.created_by_task_id)
    if owner is not None:
        return owner
    session = _load_session(repository, feedback.session_id)
    return None if session is None else _session_owner(repository, session)


def _require_feedback(
    repository: Any, feedback_id: str, user_scope_id: str,
) -> Feedback:
    feedback = _load_optional(repository, "Feedback", feedback_id, Feedback)
    if feedback is None or _feedback_owner(repository, feedback) != user_scope_id:
        raise ContractError(
            ErrorCode.RESOURCE_NOT_FOUND, "the requested feedback is not available"
        )
    return feedback


class FeedbackProtocolError(ValueError):
    """The provider replied with something this module cannot read."""


def parse_interpretation(reply: Any) -> tuple[list[dict[str, Any]], InterpretationStatus]:
    """Read only the documented keys; never execute or trust extra ones."""
    if not isinstance(reply, Mapping) or not isinstance(reply.get("symptoms"), list):
        raise FeedbackProtocolError("the reply has no symptoms list")
    parsed: list[dict[str, Any]] = []
    statuses: list[InterpretationStatus] = []
    for item in reply["symptoms"]:
        if not isinstance(item, Mapping):
            raise FeedbackProtocolError("a symptom is not an object")
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise FeedbackProtocolError("a symptom names no kind")
        status = item.get("interpretation")
        if status not in {value.value for value in InterpretationStatus}:
            raise FeedbackProtocolError("a symptom names an unknown interpretation")
        severity = item.get("severity")
        scope = item.get("range")
        if severity is not None and not isinstance(severity, str):
            raise FeedbackProtocolError("severity must be text when provided")
        if scope is not None and not isinstance(scope, str):
            raise FeedbackProtocolError("range must be text when provided")
        statuses.append(InterpretationStatus(status))
        parsed.append({
            "kind": kind,
            "severity": severity,
            "range": scope,
            "interpretation": InterpretationStatus(status),
        })
    if not parsed:
        return parsed, InterpretationStatus.uncertain
    if any(item is InterpretationStatus.explicit for item in statuses):
        return parsed, InterpretationStatus.explicit
    if any(item is InterpretationStatus.inferred for item in statuses):
        return parsed, InterpretationStatus.inferred
    return parsed, InterpretationStatus.uncertain


def _symptoms(parsed: Sequence[Mapping[str, Any]], *, original_id: str, now: str):
    evidence = [EvidenceRef(kind="local", entity_type="Feedback", entity_id=original_id)]
    symptoms = []
    for item in parsed:
        severity = item["severity"]
        scope = item["range"]
        symptoms.append(Symptom(
            kind=item["kind"],
            severity=(
                _unknown_field() if severity is None
                else _agent_field(severity, original_id=original_id, when=now)
            ),
            range=(
                _unknown_field() if scope is None
                else _agent_field(scope, original_id=original_id, when=now)
            ),
            interpretation=item["interpretation"],
            evidence_refs=evidence,
        ))
    return symptoms


class FeedbackService:
    """FBK-T01: register the words, then optionally parse them."""

    def __init__(self, repository: Any, files: Any, tasks: Any, *, clock=utc_now) -> None:
        self.repository = repository
        self.files = files
        self.tasks = tasks
        self.clock = clock

    # -- registration ------------------------------------------------------
    def precheck(self, task_input: Any, *, user_scope_id: str) -> None:
        typed = _typed_feedback(task_input)
        _require_session(self.repository, typed.session_id, user_scope_id)
        if typed.segment_id is not None:
            _require_segment(self.repository, typed.segment_id, typed.session_id)

    def record_feedback(
        self,
        task_input: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        budget: Any = None,
    ) -> TaskAccepted:
        """Save the original and register the task in one transaction."""
        from game_app.tasks import DEFAULT_ROUTES

        typed = _typed_feedback(task_input)
        registered = self.repository.find_idempotency(
            context.user_scope_id, "POST", DEFAULT_ROUTES["feedback"],
            context.idempotency_key,
        )
        if registered is None:
            self.precheck(typed, user_scope_id=context.user_scope_id)
        return self.tasks.register_task(
            typed,
            context,
            request_schema_version=request_schema_version,
            budget=budget,
            companion=lambda uow, task_id: self._save_original(uow, task_id, typed),
        )

    def _save_original(self, uow: UnitOfWork, task_id: str, typed: FeedbackInput) -> None:
        now = self.clock()
        feedback_id = _original_feedback_id(typed)
        if uow.find_object("Feedback", feedback_id) is not None:
            # A repeated re-parse of the same words reads the same original.
            return
        feedback = Feedback(
            id=feedback_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=task_id,
            session_id=typed.session_id,
            segment_id=typed.segment_id,
            raw_text=typed.raw_text,
            selections=dict(typed.selections),
            symptoms=[],
            interpretation_status=InterpretationStatus.unparsed,
        )
        uow.insert_object("Feedback", feedback)

    # -- execution ---------------------------------------------------------
    def handle(self, context: Any) -> Any:
        from game_app.contracts.enums import TaskType
        from game_app.contracts.tasks import DomainResult
        from game_app.tasks import HandlerOutcome, ProviderRejected, TaskState, TaskStopReason

        task_input = context.task_input
        if not isinstance(task_input, FeedbackInput):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the feedback handler received another task type",
            )
        original_id = _original_feedback_id(task_input)
        original = _load_optional(self.repository, "Feedback", original_id, Feedback)
        if original is None:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR,
                "the registered feedback original is missing",
            )
        context.phase("decide")
        if not context.service.provider_configured:
            return self._unparsed(
                context, original_id,
                error_code=ErrorCode.PROVIDER_NOT_CONFIGURED.value,
                note="no provider is configured; the original words are kept unparsed",
            )
        try:
            reply = context.invoke_provider(
                kind="model",
                provider_name="stub",
                payload={
                    "raw_text": original.raw_text,
                    "selections": dict(original.selections),
                    "segment_present": original.segment_id is not None,
                },
                prompt_version=MODEL_PROMPT_VERSION,
                output_schema_version=MODEL_OUTPUT_SCHEMA_VERSION,
            )
            parsed, status = parse_interpretation(reply)
        except ProviderRejected as error:
            return self._unparsed(
                context, original_id,
                error_code=ErrorCode.PROVIDER_FAILED.value,
                note=f"the provider reply was not usable: {error}",
            )
        except FeedbackProtocolError as error:
            return self._unparsed(
                context, original_id,
                error_code=ErrorCode.PROVIDER_FAILED.value,
                note=str(error),
            )

        now = self.clock()
        derived_id = _derived_feedback_id(context.task_id)
        link_id = _link_id(context.task_id)
        derived = Feedback(
            id=derived_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=context.task_id,
            session_id=original.session_id,
            segment_id=original.segment_id,
            raw_text=original.raw_text,
            selections=dict(original.selections),
            symptoms=_symptoms(parsed, original_id=original_id, now=now),
            interpretation_status=status,
        )
        link = FeedbackInterpretationLink(
            id=link_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=context.task_id,
            source_feedback_id=original_id,
            derived_feedback_id=derived_id,
            task_id=context.task_id,
        )
        with self.repository.unit_of_work() as uow:
            if uow.find_object("Feedback", derived_id) is None:
                uow.insert_object("Feedback", derived)
                uow.insert_object("FeedbackInterpretationLink", link)
                uow.insert_reference(
                    link_id, "source_feedback",
                    ObjectRef(entity_type="Feedback", entity_id=original_id),
                )
                uow.insert_reference(
                    link_id, "derived_feedback",
                    ObjectRef(entity_type="Feedback", entity_id=derived_id),
                )
        checks = [
            _check(
                "interpretation.original_kept", CheckStatus.pass_, now=now,
                note="the original feedback is unchanged",
                evidence_refs=[EvidenceRef(
                    kind="local", entity_type="Feedback", entity_id=original_id,
                )],
            ),
            _check(
                "interpretation.model", CheckStatus.pass_, now=now,
                note="the reply was read into the documented shape",
            ),
        ]
        return HandlerOutcome(
            state=TaskState.completed,
            domain_result=DomainResult(
                domain=TaskType.feedback,
                code=status.value,
                input_revision=context.task_input_revision,
                checks=checks,
            ),
            result_refs=[
                ObjectRef(entity_type="Feedback", entity_id=derived_id),
                ObjectRef(
                    entity_type="FeedbackInterpretationLink", entity_id=link_id,
                ),
            ],
        )

    def _unparsed(self, context: Any, original_id: str, *, error_code: str, note: str):
        from game_app.contracts.enums import TaskType
        from game_app.contracts.tasks import DomainResult
        from game_app.tasks import HandlerOutcome, TaskState, TaskStopReason

        now = self.clock()
        return HandlerOutcome(
            state=TaskState.partial,
            domain_result=DomainResult(
                domain=TaskType.feedback,
                code=InterpretationStatus.unparsed.value,
                input_revision=context.task_input_revision,
                checks=[_check(
                    "interpretation.model", CheckStatus.not_run, now=now, note=note,
                )],
            ),
            stop_reason=TaskStopReason(kind="execution", code="provider_error"),
            error_code=error_code,
            result_refs=[ObjectRef(entity_type="Feedback", entity_id=original_id)],
        )

    # -- reads -------------------------------------------------------------
    def get_feedback(self, feedback_id: str, user_scope_id: str) -> Feedback:
        return _require_feedback(self.repository, feedback_id, user_scope_id)


def _typed_feedback(task_input: Any) -> FeedbackInput:
    if isinstance(task_input, FeedbackInput):
        return task_input
    try:
        return FeedbackInput.model_validate_json(canonical_json(task_input))
    except Exception as error:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED, "the feedback input is invalid",
        ) from error


class PreferenceService:
    """FBK-T01: explicit preference versions with a compare-and-set guard."""

    def __init__(self, repository: Any, files: Any, *, clock=utc_now) -> None:
        self.repository = repository
        self.files = files
        self.clock = clock

    def declare_preference(
        self,
        request: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: Any = None,
    ) -> ResourceResult:
        typed = _typed_preference(request)
        request_hash = _hash(PREFERENCE_ROUTE, request_schema_version, typed)
        if self.repository.find_idempotency(
            context.user_scope_id, "POST", PREFERENCE_ROUTE, context.idempotency_key,
        ) is None:
            self._precheck(typed, context.user_scope_id)
        declaration_id = _stable_id(
            context.user_scope_id, PREFERENCE_ROUTE, context.idempotency_key,
            request_hash, "declaration",
        )
        manifest = {
            "kind": "preference_declaration",
            "request": typed.model_dump(mode="json"),
            "declaration_id": declaration_id,
        }
        published = self.files.publish(
            kind="import",
            files={f"preferences/{declaration_id}/declaration.json":
                   canonical_json(manifest)},
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=PREFERENCE_ROUTE,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_declaration(
                uow, typed, user_scope_id=context.user_scope_id,
                declaration_id=declaration_id, request_hash=request_hash,
            ),
            hooks=hooks,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _precheck(self, request: PreferenceDeclarationInput, user_scope_id: str) -> None:
        _require_feedback(self.repository, request.feedback_id, user_scope_id)
        if request.scope is PreferenceScope.game:
            if self.repository.find_object("Game", request.scope_id) is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID, "the named game does not exist",
                    field_errors=[FieldError(
                        path="scope_id", code="reference", message="unknown game",
                    )],
                )
        elif request.scope is PreferenceScope.experiment:
            if self.repository.find_object("ChangeSet", request.scope_id) is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID, "the named change set does not exist",
                    field_errors=[FieldError(
                        path="scope_id", code="reference", message="unknown change set",
                    )],
                )
        if request.series_id is not None:
            head = self._head(request.series_id)
            if head is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the preference series does not exist",
                    field_errors=[FieldError(
                        path="series_id", code="reference", message="unknown series",
                    )],
                )
            if head.id != request.expected_current_id:
                self._conflict(
                    "the preference changed; re-read the current version",
                    current=None if head is None else head.id,
                )
            if _feedback_owner(self.repository, _load_feedback(self.repository, head.feedback_id)) != user_scope_id:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "the preference series is not available",
                )

    def _write_declaration(
        self,
        uow: UnitOfWork,
        request: PreferenceDeclarationInput,
        *,
        user_scope_id: str,
        declaration_id: str,
        request_hash: str,
    ) -> list[ObjectRef]:
        now = self.clock()
        if request.series_id is None:
            series_id = _stable_id(user_scope_id, request_hash, "series")
            version = 1
            parent_id = None
        else:
            head = self._head(request.series_id, uow=uow)
            if head is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the preference series does not exist",
                )
            if head.id != request.expected_current_id:
                self._conflict(
                    "the preference changed; re-read the current version",
                    current=None if head is None else head.id,
                )
            series_id = head.series_id
            version = head.version + 1
            parent_id = head.id
        preference = Preference(
            id=(series_id if version == 1 else _stable_id(series_id, version)),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            series_id=series_id,
            version=version,
            parent_id=parent_id,
            scope=request.scope,
            scope_id=request.scope_id,
            content=request.content,
            explicitness=PreferenceExplicitness.explicit,
            lifecycle=request.lifecycle,
            feedback_id=request.feedback_id,
        )
        declaration = PreferenceDeclaration(
            id=declaration_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            input=request,
            submitted_at=now,
            source_feedback_id=request.feedback_id,
        )
        uow.insert_versioned("Preference", preference)
        uow.insert_object("PreferenceDeclaration", declaration)
        uow.insert_reference(
            declaration_id, "source_feedback",
            ObjectRef(entity_type="Feedback", entity_id=request.feedback_id),
        )
        uow.insert_reference(
            declaration_id, "preference",
            ObjectRef(entity_type="Preference", entity_id=preference.id),
        )
        return [
            ObjectRef(entity_type="Preference", entity_id=preference.id),
            ObjectRef(
                entity_type="PreferenceDeclaration", entity_id=declaration.id,
            ),
        ]

    def _head(self, series_id: str, *, uow: Any = None) -> Preference | None:
        if uow is None:
            with self.repository.unit_of_work() as work:
                return self._head(series_id, uow=work)
        rows = uow.query(
            "SELECT payload FROM objects WHERE entity_type='Preference' AND series_id=?"
            " ORDER BY version DESC, rowid DESC LIMIT 1",
            (series_id,),
        )
        if not rows:
            return None
        return Preference.model_validate_json(rows[0]["payload"])

    def _conflict(self, message: str, *, current: str | None) -> None:
        raise ContractError(
            ErrorCode.PREFERENCE_VERSION_CONFLICT,
            message,
            field_errors=[FieldError(
                path="expected_current_id", code="conflict",
                message="not the current preference version",
            )],
            details=empty_details() if current is None else empty_details(
                current_resource_ref=ObjectRef(
                    entity_type="Preference", entity_id=current,
                )
            ),
        )


class TargetAcceptanceService:
    """FBK-T01: one current acceptance per (session, proposal)."""

    def __init__(self, repository: Any, files: Any, *, clock=utc_now) -> None:
        self.repository = repository
        self.files = files
        self.clock = clock

    def record_acceptance(
        self,
        request: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: Any = None,
    ) -> ResourceResult:
        typed = _typed_acceptance(request)
        request_hash = _hash(TARGET_ACCEPTANCE_ROUTE, request_schema_version, typed)
        if self.repository.find_idempotency(
            context.user_scope_id, "POST", TARGET_ACCEPTANCE_ROUTE,
            context.idempotency_key,
        ) is None:
            self._precheck(typed, context.user_scope_id)
        acceptance_id = _stable_id(
            context.user_scope_id, TARGET_ACCEPTANCE_ROUTE, context.idempotency_key,
            request_hash, "acceptance",
        )
        manifest = {
            "kind": "target_acceptance",
            "request": typed.model_dump(mode="json"),
            "acceptance_id": acceptance_id,
        }
        published = self.files.publish(
            kind="import",
            files={f"target-acceptances/{acceptance_id}/declaration.json":
                   canonical_json(manifest)},
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=TARGET_ACCEPTANCE_ROUTE,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_acceptance(
                uow, typed, acceptance_id=acceptance_id,
            ),
            hooks=hooks,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _precheck(self, request: TargetAcceptanceInput, user_scope_id: str) -> None:
        session = _require_session(self.repository, request.session_id, user_scope_id)
        feedback = _require_feedback(self.repository, request.feedback_id, user_scope_id)
        if feedback.session_id != session.id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the feedback belongs to another session",
                field_errors=[FieldError(
                    path="feedback_id", code="reference",
                    message="feedback and session disagree",
                )],
            )
        proposal = _load_optional(
            self.repository, "TargetProposal", request.proposal_id, _target_proposal_model(),
        )
        if proposal is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID, "the target proposal does not exist",
                field_errors=[FieldError(
                    path="proposal_id", code="reference",
                    message="unknown target proposal",
                )],
            )
        owner = _task_scope(self.repository, proposal.created_by_task_id)
        if owner != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "the target proposal is not available",
            )
        self._require_session_proposal(session, request.proposal_id)

    def _require_session_proposal(self, session: PlaySession, proposal_id: str) -> None:
        if session.recommendation_id is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the session names no recommendation for this target",
                field_errors=[FieldError(
                    path="session_id", code="reference",
                    message="the session has no recommendation",
                )],
            )
        stored = self.repository.find_object(
            "RecommendationVersion", session.recommendation_id,
        )
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the session's recommendation is not available",
            )
        payload = json.loads(stored.payload_bytes)
        target = payload.get("target_proposal") or {}
        if target.get("id") != proposal_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the target proposal does not belong to the session's recommendation",
                field_errors=[FieldError(
                    path="proposal_id", code="reference",
                    message="recommendation and target disagree",
                )],
            )

    def _write_acceptance(
        self, uow: UnitOfWork, request: TargetAcceptanceInput, *, acceptance_id: str,
    ) -> list[ObjectRef]:
        now = self.clock()
        row = uow.execute(
            "SELECT * FROM target_acceptance_current WHERE session_id=? AND proposal_id=?",
            (request.session_id, request.proposal_id),
        ).fetchone()
        previous = None if row is None else row["acceptance_id"]
        if previous != request.supersedes_id:
            raise ContractError(
                ErrorCode.TARGET_ACCEPTANCE_CONFLICT,
                "the acceptance changed; re-read the current record",
                field_errors=[FieldError(
                    path="supersedes_id", code="conflict",
                    message="not the current acceptance",
                )],
                details=empty_details() if previous is None else empty_details(
                    current_resource_ref=ObjectRef(
                        entity_type="TargetAcceptance", entity_id=previous,
                    )
                ),
            )
        revision = 1 if row is None else int(row["revision"]) + 1
        acceptance = TargetAcceptance(
            id=acceptance_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            proposal_id=request.proposal_id,
            session_id=request.session_id,
            accepted=request.accepted,
            feedback_id=request.feedback_id,
            supersedes_id=request.supersedes_id,
        )
        uow.insert_object("TargetAcceptance", acceptance)
        uow.execute(
            "INSERT INTO target_acceptance_current(session_id, proposal_id, acceptance_id,"
            " revision, updated_at) VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id, proposal_id) DO UPDATE SET"
            " acceptance_id=excluded.acceptance_id, revision=excluded.revision,"
            " updated_at=excluded.updated_at",
            (request.session_id, request.proposal_id, acceptance_id, revision, now),
        )
        uow.execute(
            "INSERT INTO target_acceptance_events(session_id, proposal_id, pointer_revision,"
            " previous_acceptance_id, next_acceptance_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (request.session_id, request.proposal_id, revision, previous, acceptance_id, now),
        )
        return [ObjectRef(entity_type="TargetAcceptance", entity_id=acceptance_id)]


def _target_proposal_model():
    from game_app.contracts.recommendation import TargetProposal

    return TargetProposal


def _load_feedback(repository: Any, feedback_id: str) -> Feedback:
    stored = repository.find_object("Feedback", feedback_id)
    if stored is None:
        raise ContractError(
            ErrorCode.RESOURCE_NOT_FOUND, "the requested feedback is not available",
        )
    return Feedback.model_validate_json(stored.payload_bytes)


def _hash(route: str, schema_version: str, request: Any) -> str:
    return hashlib.sha256(canonical_json({
        "schema_version": schema_version,
        "method": "POST",
        "route": route,
        "request": request.model_dump(mode="json"),
    })).hexdigest()


def _typed_preference(request: Any) -> PreferenceDeclarationInput:
    if isinstance(request, PreferenceDeclarationInput):
        return request
    try:
        return PreferenceDeclarationInput.model_validate_json(canonical_json(request))
    except Exception as error:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED, "the preference declaration is invalid",
        ) from error


def _typed_acceptance(request: Any) -> TargetAcceptanceInput:
    if isinstance(request, TargetAcceptanceInput):
        return request
    try:
        return TargetAcceptanceInput.model_validate_json(canonical_json(request))
    except Exception as error:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED, "the target acceptance is invalid",
        ) from error


__all__ = [
    "FeedbackService",
    "PreferenceService",
    "TargetAcceptanceService",
    "parse_interpretation",
    "FeedbackProtocolError",
    "FEEDBACK_ROUTE",
    "PREFERENCE_ROUTE",
    "TARGET_ACCEPTANCE_ROUTE",
]
