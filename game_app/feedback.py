"""05 feedback: the player's words, explicit preferences and acceptance.

Three rules shape this module.  The original words are saved before any model
call and are never rewritten.  A model may produce a new, linked interpretation
of them, but it can never declare a preference or an acceptance.  A preference
version or an acceptance is compared against the current record inside the
write transaction, so a stale correction conflicts instead of overwriting.

The next-action planner (FBK-T02) adds a fourth: the *program* decides the
primary action from the 05 §6 priority table.  A model may add hypotheses, but
an ``adjust`` exists only when a real adopted base, a real old value, a closed
dependency group, a recoverable rollback and an unbroken explicit preference
all prove out, and only a ChangeSet that passed every check is ever stored.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from statistics import median
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
from game_app.contracts.configuration import AppliedConfiguration, SettingRef
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    EffectAssessment,
    InterpretationStatus,
    LegacyOutcome,
    NextActionKind,
    PlayerAcceptance,
    PreferenceExplicitness,
    PreferenceLifecycle,
    PreferenceScope,
    ValueState,
)
from game_app.contracts.feedback import (
    ChangeSet,
    Diagnosis,
    ExperimentOutcome,
    Feedback,
    FeedbackInput,
    FeedbackInterpretationLink,
    Hypothesis,
    NextActionInput,
    OutcomeInput,
    Preference,
    PreferenceDeclaration,
    PreferenceDeclarationInput,
    SettingChange,
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
NEXT_ACTION_ROUTE = "/api/next-actions"
MODEL_PROMPT_VERSION = "feedback-parse/1"
MODEL_OUTPUT_SCHEMA_VERSION = "feedback-interpretation/1"
INTERPRETATION_RULE_VERSION = "feedback-interpretation-checks/0.4"
NEXT_ACTION_RULE_VERSION = "next-action-checks/0.4"
NEXT_ACTION_PROMPT_VERSION = "next-action-plan/1"
NEXT_ACTION_OUTPUT_SCHEMA_VERSION = "next-action-hypotheses/1"
LOOP_GUARD_RULE_VERSION = "loop-guard/1"

_ID_NAMESPACE = uuid.NAMESPACE_URL
# The vocabulary in 05 §2 is advisory: ``kind`` is contractually free text, so
# an unfamiliar word is kept rather than silently rewritten or dropped.

# E-R9: only a product option can state satisfaction.  Free text cannot.
_SATISFACTION_KEY = "feeling"
_SATISFIED = "satisfied"
_PROBLEM = "problem"
# A symptom whose first checks are public version/support facts (05 §6).
_RESEARCH_SYMPTOMS = frozenset({"low_rate", "ghosting"})
_SYMPTOM_CHECK_ORDER = {
    "low_rate": "stream, limiter policy, resolution and the available timing",
    "stutter": "the segment, loading and repeatability",
    "blur": "input/output resolution and reconstruction",
    "ghosting": "motion and the game/version",
    "input_feel": "subjective feel, never displayed FPS",
    "scene_specific": "evidence from that exact scene",
}
# E-R12: a critical unknown blocks ``adjust``; the restart hint is not critical.
_CRITICAL_CHECKS = (
    "change_set.base",
    "change_set.direction",
    "change_set.old_value",
    "change_set.new_value_evidence",
    "change_set.dependency_group",
    "change_set.preference",
    "change_set.rollback",
)



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

    def get_preference(self, preference_id: str, user_scope_id: str) -> Preference:
        """One visible preference version; another player's is not found."""
        stored = self.repository.find_object("Preference", preference_id)
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the preference is not available"
            )
        preference = Preference.model_validate_json(stored.payload_bytes)
        feedback = _load_feedback(self.repository, preference.feedback_id)
        if _feedback_owner(self.repository, feedback) != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the preference is not available"
            )
        return preference

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


def _plan_check(
    check_id: str, status: CheckStatus, *, now: str, note: str,
    evidence_refs: Sequence[EvidenceRef] = (),
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=NEXT_ACTION_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=list(evidence_refs),
        checked_at=None if status is CheckStatus.not_run else now,
        method="deterministic next-action rule",
        note=note,
    )


def _loop_check(
    status: CheckStatus, *, now: str, note: str,
) -> CheckResult:
    return CheckResult(
        check_id="loop_guard",
        definition_version=LOOP_GUARD_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else now,
        method="deterministic loop guard",
        note=note,
    )


def _typed_next_action(task_input: Any) -> NextActionInput:
    if isinstance(task_input, NextActionInput):
        return task_input
    try:
        return NextActionInput.model_validate_json(canonical_json(task_input))
    except Exception as error:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED, "the next-action input is invalid",
        ) from error


def _require_applied(
    repository: Any, applied_id: str, user_scope_id: str,
) -> AppliedConfiguration:
    stored = repository.find_object("AppliedConfiguration", applied_id)
    if stored is None:
        raise ContractError(
            ErrorCode.RESOURCE_NOT_FOUND, "the adopted configuration is not available"
        )
    applied = AppliedConfiguration.model_validate_json(stored.payload_bytes)
    if applied.user_scope_id != user_scope_id:
        # Another player's configuration is not merely hidden: it does not exist.
        raise ContractError(
            ErrorCode.RESOURCE_NOT_FOUND, "the adopted configuration is not available"
        )
    return applied


def _latest_by_series(payloads: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    heads: dict[str, Mapping[str, Any]] = {}
    for item in payloads:
        series = item.get("series_id") or item["id"]
        current = heads.get(series)
        if current is None or (
            int(item.get("version") or 1), item["id"]
        ) > (
            int(current.get("version") or 1), current["id"]
        ):
            heads[series] = item
    return list(heads.values())


def _same_setting_value(left: Any, right: Any) -> bool:
    """Strict scalar equality: ``True`` and ``1`` are not the same setting value."""
    return type(left) is type(right) and left == right


def _direction(old: Any, new: Any) -> str:
    if isinstance(old, bool) or isinstance(new, bool):
        if old == new:
            return "unchanged"
        return "enable" if new else "disable"
    if (
        isinstance(old, (int, float)) and isinstance(new, (int, float))
        and not isinstance(old, bool) and not isinstance(new, bool)
    ):
        if new > old:
            return "increase"
        if new < old:
            return "decrease"
        return "unchanged"
    return "change"


def _program_hypothesis(
    task_id: str, *, kind: NextActionKind, statement: str,
    feedback_ids: Sequence[str], unknowns: Sequence[str], test_action: str,
) -> Hypothesis:
    return Hypothesis(
        id=_stable_id("hypothesis", task_id, kind.value, statement),
        statement=statement,
        support=[
            EvidenceRef(kind="local", entity_type="Feedback", entity_id=item)
            for item in feedback_ids
        ],
        counter=[],
        unknowns=list(unknowns),
        test_action=test_action,
    )


class NextActionService:
    """FBK-T02: decide one primary action and validate a legal ChangeSet."""

    def __init__(
        self,
        repository: Any,
        files: Any,
        tasks: Any,
        *,
        adoption: Any | None = None,
        performance: Any | None = None,
        knowledge: Any | None = None,
        clock=utc_now,
    ) -> None:
        self.repository = repository
        self.files = files
        self.tasks = tasks
        self.adoption = adoption
        self.performance = performance
        self.knowledge = knowledge
        self.clock = clock

    # -- registration ------------------------------------------------------
    def precheck(self, task_input: Any, *, user_scope_id: str) -> None:
        typed = _typed_next_action(task_input)
        session = _require_session(self.repository, typed.session_id, user_scope_id)
        for feedback_id in typed.feedback_ids:
            feedback = _require_feedback(self.repository, feedback_id, user_scope_id)
            if feedback.session_id != session.id:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the feedback belongs to another session",
                    field_errors=[FieldError(
                        path="feedback_ids", code="reference",
                        message="feedback and session disagree",
                    )],
                )
        if typed.current_applied_id is not None:
            applied = _require_applied(
                self.repository, typed.current_applied_id, user_scope_id,
            )
            if applied.installation_id != session.installation_id:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the adopted configuration belongs to another installation",
                    field_errors=[FieldError(
                        path="current_applied_id", code="reference",
                        message="installation mismatch",
                    )],
                )
        if typed.package_id is not None and self.knowledge is not None:
            self.knowledge.get_package(
                typed.package_id, user_scope_id=user_scope_id, verify_artifacts=False,
            )
        if self.performance is not None:
            for summary_id in typed.summary_ids:
                self.performance.get_summary(summary_id, user_scope_id)
            for binding in typed.bindings:
                self.performance.require_analyzeable(binding, user_scope_id)

    def plan_next_action(
        self,
        task_input: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        budget: Any = None,
    ) -> TaskAccepted:
        from game_app.tasks import DEFAULT_ROUTES

        typed = _typed_next_action(task_input)
        registered = self.repository.find_idempotency(
            context.user_scope_id, "POST", DEFAULT_ROUTES["next_action"],
            context.idempotency_key,
        )
        if registered is None:
            self.precheck(typed, user_scope_id=context.user_scope_id)
        return self.tasks.register_task(
            typed,
            context,
            request_schema_version=request_schema_version,
            budget=budget,
        )

    # -- execution ---------------------------------------------------------
    def handle(self, context: Any) -> Any:
        from game_app.contracts.enums import TaskType
        from game_app.contracts.tasks import DomainResult
        from game_app.tasks import (
            HandlerOutcome,
            ProviderRejected,
            TaskState,
            TaskStopReason,
        )

        typed = context.task_input
        if not isinstance(typed, NextActionInput):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the next-action handler received another task type",
            )
        now = self.clock()
        session = _require_session(
            self.repository, typed.session_id, context.user_scope_id,
        )
        feedbacks = [
            _require_feedback(self.repository, item, context.user_scope_id)
            for item in typed.feedback_ids
        ]
        applied = (
            None if typed.current_applied_id is None
            else _require_applied(
                self.repository, typed.current_applied_id, context.user_scope_id,
            )
        )
        symptoms = sorted({
            symptom.kind for feedback in feedbacks for symptom in feedback.symptoms
        })
        context.phase("decide")

        model_checks: list[CheckResult] = []
        model_hypotheses: list[Hypothesis] = []
        provider_failed = False
        if not context.service.provider_configured:
            model_checks.append(_plan_check(
                "interpretation.model", CheckStatus.not_run, now=now,
                note="no provider is configured; only deterministic rules were used",
            ))
        else:
            try:
                reply = context.invoke_provider(
                    kind="model",
                    provider_name="stub",
                    payload={
                        "symptoms": symptoms,
                        "selections": [
                            dict(feedback.selections) for feedback in feedbacks
                        ],
                        "feedback_present": bool(feedbacks),
                        "baseline_present": applied is not None,
                    },
                    prompt_version=NEXT_ACTION_PROMPT_VERSION,
                    output_schema_version=NEXT_ACTION_OUTPUT_SCHEMA_VERSION,
                )
                model_hypotheses = _parse_hypotheses(reply, task_id=context.task_id)
                model_checks.append(_plan_check(
                    "interpretation.model", CheckStatus.pass_, now=now,
                    note="the reply was read for hypotheses only; the action is"
                         " decided by the priority table",
                ))
            except (ProviderRejected, _NextActionProtocolError) as error:
                provider_failed = True
                model_checks.append(_plan_check(
                    "interpretation.model", CheckStatus.not_run, now=now,
                    note=f"the provider reply was not usable: {error}",
                ))

        decision = self._decide(
            context, typed, session, feedbacks, applied, symptoms, now,
        )
        primary = decision.action
        change_set = None
        checks = list(model_checks) + list(decision.checks)
        if primary is NextActionKind.adjust:
            change_set = decision.change_set
            if change_set is None:
                raise ContractError(
                    ErrorCode.INTERNAL_ERROR,
                    "an adjust decision produced no ChangeSet",
                )
        program_hypothesis = _program_hypothesis(
            context.task_id,
            kind=primary,
            statement=decision.statement,
            feedback_ids=typed.feedback_ids,
            unknowns=decision.unknowns,
            test_action=decision.test_action,
        )
        hypotheses = model_hypotheses + [program_hypothesis]
        diagnosis = Diagnosis(
            id=_stable_id("diagnosis", context.task_id, "1"),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=context.task_id,
            input=typed,
            hypotheses=hypotheses,
            primary_action=primary,
            change_set_id=None if change_set is None else change_set.id,
            checks=checks,
        )
        self._store(typed, diagnosis, change_set, session=session)
        refs = [ObjectRef(entity_type="Diagnosis", entity_id=diagnosis.id)]
        if change_set is not None:
            refs.append(ObjectRef(entity_type="ChangeSet", entity_id=change_set.id))
        domain_checks = checks + [CheckResult(
            check_id="primary_action",
            definition_version=NEXT_ACTION_RULE_VERSION,
            status=CheckStatus.pass_,
            input_refs=[],
            evidence_refs=[],
            checked_at=now,
            method="deterministic 05 §6 priority table",
            note=primary.value,
        )]
        return HandlerOutcome(
            state=TaskState.partial if provider_failed else TaskState.completed,
            domain_result=DomainResult(
                domain=TaskType.next_action,
                code=primary.value,
                input_revision=context.task_input_revision,
                checks=domain_checks,
            ),
            stop_reason=(
                None if not provider_failed
                else TaskStopReason(kind="execution", code="provider_error")
            ),
            error_code=(
                None if not provider_failed else ErrorCode.PROVIDER_FAILED.value
            ),
            result_refs=refs,
        )

    # -- decision ----------------------------------------------------------
    def _decide(
        self, context: Any, typed: NextActionInput, session: PlaySession,
        feedbacks: Sequence[Feedback], applied: AppliedConfiguration | None,
        symptoms: Sequence[str], now: str,
    ) -> "_Decision":
        checks: list[CheckResult] = []
        gaps: list[str] = []

        latest = _latest_feedback(feedbacks)
        satisfied = (
            latest is not None
            and latest.selections.get(_SATISFACTION_KEY) == _SATISFIED
            and not any(
                feedback.selections.get(_SATISFACTION_KEY) == _PROBLEM
                for feedback in feedbacks
            )
        )
        if satisfied:
            return _Decision(
                action=NextActionKind.keep,
                statement="the player explicitly reports satisfaction; no change is"
                          " needed without a reported risk",
                unknowns=[],
                test_action="",
                checks=[_plan_check(
                    "satisfaction.explicit", CheckStatus.pass_, now=now,
                    note="the latest feedback selection is feeling=satisfied and no"
                         " feedback reports a problem",
                )],
            )
        checks.append(_plan_check(
            "satisfaction.explicit", CheckStatus.unknown, now=now,
            note="the player did not state an explicit satisfaction option",
        ))

        candidate, chain_gaps = self._candidate_changes(applied, session, now)
        if candidate is None:
            checks.extend(self._base_checks(applied, now))
            gaps.extend(chain_gaps)
            return self._without_adjust(
                typed, symptoms, gaps, checks, now,
            )

        checks.extend(candidate.checks)
        gaps.extend(candidate.gaps)
        critical_ok = all(
            check.status is CheckStatus.pass_
            for check in candidate.checks
            if check.check_id in _CRITICAL_CHECKS
        )
        if not critical_ok:
            gaps.append("the change could not prove every critical condition")
            return self._without_adjust(typed, symptoms, gaps, checks, now)

        loop = self._loop_guard(applied, candidate, typed, now)
        checks.append(loop)
        if loop.status is not CheckStatus.pass_:
            # 05 §7: an old refused direction is not repeated without new
            # evidence, and a measurement would be the same pointless retry.
            return _Decision(
                action=NextActionKind.insufficient_evidence,
                statement="the same change direction was already refused and no"
                          " newer recommendation or measurement exists",
                unknowns=list(gaps) + [
                    "retrying this direction would need a new recommendation"
                    " version or a new measurement"
                ],
                test_action="",
                checks=list(checks),
            )

        changes = [
            SettingChange(
                setting_ref=item.action.setting_ref,
                old_value=item.old_value,
                new_value=item.action.value.value,
                unit=item.action.value.unit,
                dependency_group_id=item.action.dependency_group_id,
                restart_required=item.action.restart_required,
            )
            for item in candidate.actions
        ]
        change_set = ChangeSet(
            id=_stable_id(
                "change-set", context.task_id,
                canonical_json([
                    {
                        "setting_ref": change.setting_ref.model_dump(mode="json"),
                        "old_value": change.old_value.model_dump(mode="json"),
                        "new_value": change.new_value,
                    }
                    for change in changes
                ]).decode("utf-8"),
            ),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=context.task_id,
            base_applied_id=applied.id,
            installation_id=applied.installation_id,
            changes=changes,
            hypothesis_id=_stable_id(
                "hypothesis", context.task_id, NextActionKind.adjust.value,
                self._statement_for(symptoms, candidate),
            ),
            evidence_refs=candidate.evidence_refs,
            protected_preference_ids=candidate.protected_preference_ids,
            expected_direction={
                item.action.setting_ref.key: _direction(
                    item.old_value.value, item.action.value.value,
                )
                for item in candidate.actions
            },
            protocol_id=session.test_protocol_id,
            rollback_ref=applied.id,
            checks=candidate.checks,
        )
        return _Decision(
            action=NextActionKind.adjust,
            statement=self._statement_for(symptoms, candidate),
            unknowns=[],
            test_action="repeat the same scenario and protocol after applying the"
                        " change, then compare the same metrics",
            checks=list(candidate.checks) + [loop],
            change_set=change_set,
        )

    def _without_adjust(
        self, typed: NextActionInput, symptoms: Sequence[str],
        gaps: Sequence[str], checks: Sequence[CheckResult], now: str,
    ) -> "_Decision":
        """The 05 §6 fallback: measure, research, or stop with the gap stated."""
        needs_baseline = typed.current_applied_id is None
        needs_measurement = bool(symptoms) or bool(typed.feedback_ids)
        research = (
            typed.package_id is None
            and any(item in _RESEARCH_SYMPTOMS for item in symptoms)
        )
        if research:
            action = NextActionKind.research
            statement = (
                "the direction depends on public version/support information that"
                " the current package does not contain"
            )
            unknowns = list(gaps) + [
                "the game build, limiter policy and version notes are not in evidence"
            ]
            test_action = "collect the named public version and support information"
        elif needs_measurement or needs_baseline:
            action = NextActionKind.measure
            statement = (
                "the available evidence cannot separate the explanations; one"
                " controlled measurement is needed"
            )
            unknowns = list(gaps) or [
                "there is no measured baseline for this session"
            ]
            test_action = (
                "record the same scenario twice with the same test protocol and"
                " compare the same metrics"
            )
        else:
            action = NextActionKind.insufficient_evidence
            statement = "no executable or measurable direction is supported by evidence"
            unknowns = list(gaps) or [
                "no feedback, summary or adopted baseline was supplied"
            ]
            test_action = ""
        return _Decision(
            action=action, statement=statement, unknowns=unknowns,
            test_action=test_action, checks=list(checks),
        )

    def _base_checks(
        self, applied: AppliedConfiguration | None, now: str,
    ) -> list[CheckResult]:
        if applied is None:
            return [_plan_check(
                "change_set.base", CheckStatus.fail, now=now,
                note="no adopted baseline is named; a formal ChangeSet is impossible",
            )]
        return [_plan_check(
            "change_set.base", CheckStatus.pass_, now=now,
            note="the named adopted configuration is frozen and owned",
            evidence_refs=[EvidenceRef(
                kind="local", entity_type="AppliedConfiguration",
                entity_id=applied.id,
            )],
        )]

    # -- candidate construction -------------------------------------------
    def _candidate_changes(
        self, applied: AppliedConfiguration | None, session: PlaySession, now: str,
    ) -> tuple["_Candidate | None", list[str]]:
        if applied is None:
            return None, ["there is no adopted baseline"]
        if applied.recommendation_id is None:
            return None, [
                "the adopted configuration names no recommendation; no setting"
                " direction can be derived"
            ]
        from game_app.contracts.recommendation import (
            RecommendationActionKind,
            RecommendationVersion,
        )

        stored = self.repository.find_object(
            "RecommendationVersion", applied.recommendation_id,
        )
        if stored is None:
            return None, ["the adopted configuration's recommendation is missing"]
        version = RecommendationVersion.model_validate_json(stored.payload_bytes)
        executable = [
            action for action in version.actions
            if action.action is RecommendationActionKind.set_value
            and action.value is not None
            and action.value.value_kind == "fixed"
        ]
        if not executable:
            return None, [
                "the adopted recommendation contains no fixed set-value action"
            ]
        first = executable[0]
        group = first.dependency_group_id
        if group is None:
            included = [first]
        else:
            included = [
                action for action in executable
                if action.dependency_group_id == group
            ]
            unusable = [
                action for action in version.actions
                if action.dependency_group_id == group
                and action not in included
            ]
        by_key = {setting.setting_ref.key: setting for setting in applied.settings}
        checks: list[CheckResult] = [_plan_check(
            "change_set.base", CheckStatus.pass_, now=now,
            note="the named adopted configuration is frozen and owned",
            evidence_refs=[EvidenceRef(
                kind="local", entity_type="AppliedConfiguration",
                entity_id=applied.id,
            )],
        )]
        gaps: list[str] = []
        items: list[_CandidateAction] = []
        old_ok = True
        old_unknown = False
        for action in included:
            entry = by_key.get(action.setting_ref.key)
            if entry is None:
                old_ok = False
                gaps.append(
                    f"the base configuration has no value for {action.setting_ref.key}"
                )
                continue
            if entry.value.value_state is not ValueState.known:
                old_unknown = True
                gaps.append(
                    f"the base value of {action.setting_ref.key} is unknown"
                )
                continue
            items.append(_CandidateAction(action=action, old_value=entry.value))
        effective = [
            item for item in items
            if not _same_setting_value(
                item.old_value.value, item.action.value.value,
            )
        ]
        checks.append(_plan_check(
            "change_set.direction",
            CheckStatus.pass_ if effective else CheckStatus.fail,
            now=now,
            note=(
                "every change moves the value away from the frozen base"
                if effective else
                "the recommendation's value already equals the frozen base;"
                " there is no direction to change"
            ),
        ))
        if not effective:
            gaps.append("there is no real direction to change")
        if old_ok and not old_unknown and len(items) == len(included):
            checks.append(_plan_check(
                "change_set.old_value", CheckStatus.pass_, now=now,
                note="every old value equals the frozen base",
            ))
        elif old_ok and old_unknown:
            checks.append(_plan_check(
                "change_set.old_value", CheckStatus.unknown, now=now,
                note="an old value is unknown; confirm it before adopting",
            ))
        else:
            checks.append(_plan_check(
                "change_set.old_value", CheckStatus.fail, now=now,
                note="the base does not contain every changed setting",
            ))

        evidence_ok = all(bool(item.action.evidence_refs) for item in effective)
        checks.append(_plan_check(
            "change_set.new_value_evidence",
            CheckStatus.pass_ if evidence_ok else CheckStatus.unknown,
            now=now,
            note=(
                "each new value carries its recommendation evidence"
                if evidence_ok else
                "a new value has no 02 evidence or local basis"
            ),
        ))
        if not evidence_ok:
            gaps.append("a new value has no evidence")

        if group is None:
            checks.append(_plan_check(
                "change_set.dependency_group", CheckStatus.pass_, now=now,
                note="one setting with no declared dependency group",
            ))
        elif unusable:
            checks.append(_plan_check(
                "change_set.dependency_group", CheckStatus.unknown, now=now,
                note="the declared group contains an action that is not a fixed"
                     " value; the group cannot close",
            ))
            gaps.append("the dependency group cannot close")
        elif old_unknown or len(items) != len(included):
            checks.append(_plan_check(
                "change_set.dependency_group", CheckStatus.unknown, now=now,
                note="the dependency group is not closed: a member has no known"
                     " old value",
            ))
            gaps.append("the dependency group is not closed")
        else:
            checks.append(_plan_check(
                "change_set.dependency_group", CheckStatus.pass_, now=now,
                note="every member of the declared group is included and has a"
                     " known old value",
            ))

        protected = self._protected_preferences(applied.user_scope_id, session.game_id)
        if protected:
            checks.append(_plan_check(
                "change_set.preference", CheckStatus.unknown, now=now,
                note="active explicit preference(s) "
                     + ", ".join(item.id for item in protected)
                     + " cannot be proven compatible with this change",
            ))
            gaps.append(
                "an active explicit preference may conflict: "
                + ", ".join(item.id for item in protected)
            )
        else:
            checks.append(_plan_check(
                "change_set.preference", CheckStatus.pass_, now=now,
                note="no active explicit global or same-game preference is in scope",
            ))

        restart_unknown = any(
            action.restart_required.value_state is not ValueState.known
            for action in included
        )
        checks.append(_plan_check(
            "change_set.restart",
            CheckStatus.unknown if restart_unknown else CheckStatus.pass_,
            now=now,
            note=(
                "the restart requirement is unknown; tell the player before applying"
                if restart_unknown else "the restart requirement is stated"
            ),
        ))

        rollback_ok = (
            applied.installation_id is not None
            and old_ok and not old_unknown
            and len(items) == len(included)
        )
        checks.append(_plan_check(
            "change_set.rollback", CheckStatus.pass_ if rollback_ok else (
                CheckStatus.unknown if old_unknown else CheckStatus.fail
            ),
            now=now,
            note=(
                "the frozen base is a same-installation AppliedConfiguration and"
                " every old value is recoverable"
                if rollback_ok else
                "the old value is unknown, so no real rollback step exists"
            ),
        ))
        if not rollback_ok:
            gaps.append("the rollback target is not proven recoverable")

        evidence: list[EvidenceRef] = []
        for item in effective:
            for evidence_ref in item.action.evidence_refs:
                if evidence_ref not in evidence:
                    evidence.append(evidence_ref)
        if not rollback_ok or old_unknown or len(items) != len(included):
            # The candidate exists for reporting, but it can never be adopted.
            return _Candidate(
                actions=effective, checks=checks, gaps=gaps,
                evidence_refs=evidence, protected_preference_ids=[],
            ), gaps
        return _Candidate(
            actions=effective, checks=checks, gaps=gaps, evidence_refs=evidence,
            protected_preference_ids=[item.id for item in protected],
        ), gaps

    def _protected_preferences(
        self, user_scope_id: str, game_id: str,
    ) -> list[Preference]:
        with self.repository.unit_of_work() as uow:
            rows = uow.query(
                "SELECT payload FROM objects WHERE entity_type='Preference'"
                " ORDER BY created_at, rowid"
            )
        payloads = [json.loads(row["payload"]) for row in rows]
        protected: list[Preference] = []
        for payload in _latest_by_series(payloads):
            preference = Preference.model_validate_json(canonical_json(payload))
            if preference.lifecycle is not PreferenceLifecycle.active:
                continue
            if preference.explicitness is not PreferenceExplicitness.explicit:
                # E-R11: a tentative reading never protects a setting.
                continue
            in_scope = (
                preference.scope is PreferenceScope.global_
                or (
                    preference.scope is PreferenceScope.game
                    and preference.scope_id == game_id
                )
            )
            if not in_scope:
                continue
            try:
                feedback = _load_feedback(self.repository, preference.feedback_id)
                owner = _feedback_owner(self.repository, feedback)
            except ContractError:
                continue
            if owner == user_scope_id:
                protected.append(preference)
        return protected

    def _statement_for(
        self, symptoms: Sequence[str], candidate: "_Candidate",
    ) -> str:
        keys = ", ".join(item.action.setting_ref.key for item in candidate.actions)
        if symptoms:
            return (
                f"the reported {', '.join(symptoms)} may be reduced by changing"
                f" {keys} in the recorded direction"
            )
        return f"changing {keys} in the recorded direction may address the report"

    # -- loop guard --------------------------------------------------------
    def _loop_guard(
        self, applied: AppliedConfiguration | None, candidate: "_Candidate",
        typed: NextActionInput, now: str,
    ) -> CheckResult:
        if applied is None:
            return _loop_check(
                CheckStatus.pass_, now=now, note="no baseline, so no old attempt exists",
            )
        prior = self._prior_attempts(applied, candidate)
        if not prior:
            return _loop_check(
                CheckStatus.pass_, now=now,
                note="no earlier attempt in this direction is recorded",
            )
        refused = [item for item in prior if item["refused"]]
        if not refused:
            return _loop_check(
                CheckStatus.pass_, now=now,
                note="earlier attempts in this direction were not refused",
            )
        if self._new_evidence(applied, refused, typed):
            return _loop_check(
                CheckStatus.pass_, now=now,
                note="a new recommendation version or summary is newer than the"
                     " refused attempt; retrying is legitimate",
            )
        return _loop_check(
            CheckStatus.fail, now=now,
            note="the same direction was already refused ("
                 + ", ".join(item["outcome_id"] for item in refused)
                 + ") and no newer evidence exists",
        )

    def _prior_attempts(
        self, applied: AppliedConfiguration, candidate: "_Candidate",
    ) -> list[dict[str, Any]]:
        keys = {item.action.setting_ref.key for item in candidate.actions}
        direction = {
            item.action.setting_ref.key: _direction(
                item.old_value.value, item.action.value.value,
            )
            for item in candidate.actions
        }
        with self.repository.unit_of_work() as uow:
            change_rows = uow.query(
                "SELECT payload FROM objects WHERE entity_type='ChangeSet'"
                " ORDER BY created_at, rowid"
            )
            outcome_rows = uow.query(
                "SELECT payload FROM objects WHERE entity_type='ExperimentOutcome'"
                " ORDER BY created_at, rowid"
            )
        outcomes = [json.loads(row["payload"]) for row in outcome_rows]
        attempts: list[dict[str, Any]] = []
        for row in change_rows:
            change_set = json.loads(row["payload"])
            if change_set.get("installation_id") != applied.installation_id:
                continue
            if set(change_set.get("expected_direction") or {}) != keys:
                continue
            if change_set.get("expected_direction") != direction:
                continue
            base_id = change_set.get("base_applied_id")
            base = self.repository.find_object("AppliedConfiguration", base_id or "")
            if base is None:
                continue
            if AppliedConfiguration.model_validate_json(
                base.payload_bytes
            ).series_id != applied.series_id:
                continue
            for outcome in outcomes:
                if outcome.get("change_set_id") != change_set.get("id"):
                    continue
                refused = (
                    outcome.get("effect_assessment") in {"worse", "no_clear_change"}
                    or outcome.get("legacy_outcome") in {
                        "worse", "no_clear_change", "improved_but_rejected",
                    }
                    or outcome.get("player_acceptance") == "rejected"
                )
                attempts.append({
                    "change_set_id": change_set.get("id"),
                    "outcome_id": outcome.get("id"),
                    "created_at": outcome.get("created_at"),
                    "refused": refused,
                    "base_applied_id": base_id,
                })
        return attempts

    def _new_evidence(
        self, applied: AppliedConfiguration, refused: Sequence[Mapping[str, Any]],
        typed: NextActionInput,
    ) -> bool:
        from game_app.contracts.recommendation import RecommendationVersion

        current_recommendation = applied.recommendation_id
        for item in refused:
            base = self.repository.find_object(
                "AppliedConfiguration", item["base_applied_id"] or "",
            )
            if base is None:
                continue
            previous = AppliedConfiguration.model_validate_json(base.payload_bytes)
            if (
                current_recommendation is not None
                and previous.recommendation_id is not None
                and current_recommendation != previous.recommendation_id
            ):
                return True
        newest = max(
            (item["created_at"] for item in refused if item.get("created_at")),
            default=None,
        )
        if newest is None:
            return False
        if current_recommendation is not None:
            stored = self.repository.find_object(
                "RecommendationVersion", current_recommendation,
            )
            if stored is not None:
                version = RecommendationVersion.model_validate_json(stored.payload_bytes)
                if version.created_at > newest:
                    return True
        for summary_id in typed.summary_ids:
            stored = self.repository.find_object("PerformanceSummary", summary_id)
            if stored is not None:
                payload = json.loads(stored.payload_bytes)
                if payload.get("created_at", "") > newest:
                    return True
        return False

    # -- write -------------------------------------------------------------
    def _store(
        self, typed: NextActionInput, diagnosis: Diagnosis,
        change_set: ChangeSet | None, *, session: PlaySession,
    ) -> None:
        with self.repository.unit_of_work() as uow:
            if uow.find_object("Diagnosis", diagnosis.id) is not None:
                return
            uow.insert_object("Diagnosis", diagnosis)
            uow.insert_reference(diagnosis.id, "session", ObjectRef(
                entity_type="PlaySession", entity_id=session.id,
            ))
            for feedback_id in typed.feedback_ids:
                uow.insert_reference(diagnosis.id, "feedback", ObjectRef(
                    entity_type="Feedback", entity_id=feedback_id,
                ))
            if typed.current_applied_id is not None:
                uow.insert_reference(diagnosis.id, "applied_configuration", ObjectRef(
                    entity_type="AppliedConfiguration",
                    entity_id=typed.current_applied_id,
                ))
            if typed.package_id is not None:
                uow.insert_reference(diagnosis.id, "package", ObjectRef(
                    entity_type="KnowledgePackage", entity_id=typed.package_id,
                ))
            for summary_id in typed.summary_ids:
                uow.insert_reference(diagnosis.id, "summary", ObjectRef(
                    entity_type="PerformanceSummary", entity_id=summary_id,
                ))
            if change_set is None:
                return
            uow.insert_object("ChangeSet", change_set)
            uow.insert_reference(change_set.id, "diagnosis", ObjectRef(
                entity_type="Diagnosis", entity_id=diagnosis.id,
            ))
            uow.insert_reference(change_set.id, "base_applied", ObjectRef(
                entity_type="AppliedConfiguration",
                entity_id=change_set.base_applied_id,
            ))
            uow.insert_reference(change_set.id, "installation", ObjectRef(
                entity_type="GameInstallation",
                entity_id=change_set.installation_id,
            ))


    # -- reads -------------------------------------------------------------
    def get_diagnosis(self, diagnosis_id: str, user_scope_id: str) -> Diagnosis:
        stored = self.repository.find_object("Diagnosis", diagnosis_id)
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the diagnosis is not available"
            )
        diagnosis = Diagnosis.model_validate_json(stored.payload_bytes)
        if _task_scope(self.repository, diagnosis.created_by_task_id) != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the diagnosis is not available"
            )
        return diagnosis

    def get_change_set(self, change_set_id: str, user_scope_id: str) -> ChangeSet:
        stored = self.repository.find_object("ChangeSet", change_set_id)
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the change set is not available"
            )
        change_set = ChangeSet.model_validate_json(stored.payload_bytes)
        if _task_scope(self.repository, change_set.created_by_task_id) != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the change set is not available"
            )
        return change_set


OUTCOME_ROUTE = "/api/outcomes"
EFFECT_POLICY_VERSION = "effect-policy/1"
OUTCOME_RULE_VERSION = "outcome-checks/0.4"
_MIN_ROUNDS = 3
# E-R20: metric direction is fixed by the packaged rule, never re-read from a
# client.  FPS-like rates improve as they rise; interval (ms) tails improve as
# they fall.
_HIGHER_IS_BETTER = frozenset({
    "average_present_rate", "slowest_1pct_inverse_mean",
})
_LOWER_IS_BETTER = frozenset({
    "interval_p95_nearest_rank", "interval_p99_nearest_rank",
})
_PRIMARY_ORDER = (
    "average_present_rate",
    "slowest_1pct_inverse_mean",
    "interval_p99_nearest_rank",
    "interval_p95_nearest_rank",
)


def _typed_outcome(request: Any) -> OutcomeInput:
    if isinstance(request, OutcomeInput):
        return request
    try:
        return OutcomeInput.model_validate_json(canonical_json(request))
    except Exception as error:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED, "the outcome input is invalid",
        ) from error


def _change_set_owner(repository: Any, change_set: ChangeSet) -> str | None:
    owner = _task_scope(repository, change_set.created_by_task_id)
    if owner is not None:
        return owner
    # A ChangeSet registered by a synchronous host (or an offline fixture) is
    # owned through its frozen base, which is a real same-installation record.
    stored = repository.find_object("AppliedConfiguration", change_set.base_applied_id)
    return None if stored is None else stored.payload.get("user_scope_id")


def _require_change_set(
    repository: Any, change_set_id: str, user_scope_id: str,
) -> ChangeSet:
    stored = repository.find_object("ChangeSet", change_set_id)
    if stored is None:
        raise ContractError(
            ErrorCode.RESOURCE_NOT_FOUND, "the change set is not available"
        )
    change_set = ChangeSet.model_validate_json(stored.payload_bytes)
    if _change_set_owner(repository, change_set) != user_scope_id:
        raise ContractError(
            ErrorCode.RESOURCE_NOT_FOUND, "the change set is not available"
        )
    return change_set


def _binding_key(item: Any) -> tuple[str, int, int]:
    return (item.segment_id, item.segment_revision, item.binding_revision)


def _legacy_reduction(
    acceptance: PlayerAcceptance, effect: EffectAssessment,
) -> LegacyOutcome:
    """state §5.2 exactly: acceptance and effect stay separate dimensions."""
    if acceptance is PlayerAcceptance.accepted:
        return LegacyOutcome.accepted
    if acceptance is PlayerAcceptance.rejected and effect is EffectAssessment.improved:
        return LegacyOutcome.improved_but_rejected
    if effect is EffectAssessment.no_clear_change:
        return LegacyOutcome.no_clear_change
    if effect is EffectAssessment.worse:
        return LegacyOutcome.worse
    return LegacyOutcome.inconclusive


def _outcome_check(check_id: str, status: CheckStatus, *, now: str, note: str):
    return CheckResult(
        check_id=check_id,
        definition_version=OUTCOME_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else now,
        method="deterministic outcome rule",
        note=note,
    )


@dataclass(frozen=True)
class _MetricRound:
    definition_id: str
    metric_key: str
    direction: str
    before_median: float
    after_median: float
    before_range: float
    after_range: float
    delta: float
    beyond_fluctuation: bool


class OutcomeService:
    """FBK-T03: verify the adopted chain, then reduce an immutable outcome."""

    def __init__(
        self, repository: Any, files: Any, *, performance: Any, clock=utc_now,
    ) -> None:
        self.repository = repository
        self.files = files
        self.performance = performance
        self.clock = clock

    # -- entry -------------------------------------------------------------
    def record_outcome(
        self,
        request: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
    ) -> ResourceResult:
        typed = _typed_outcome(request)
        request_hash = _hash(OUTCOME_ROUTE, request_schema_version, typed)
        with self.repository.unit_of_work() as uow:
            replay = _replay_result(
                uow, context, OUTCOME_ROUTE, request_schema_version, request_hash,
            )
            if replay is not None:
                return replay
        # Verification strictly precedes any write: a pure conflict never
        # consumes the key and never leaves a false Outcome.
        prepared = self.precheck(typed, context.user_scope_id)
        outcome_id = _stable_id(
            context.user_scope_id, OUTCOME_ROUTE, context.idempotency_key,
            request_hash, "outcome",
        )
        now = self.clock()
        evaluation = self._evaluate(typed, prepared, outcome_id=outcome_id, now=now)
        data = canonical_json(evaluation["manifest"])
        relative_name = f"outcomes/{outcome_id}/evaluation.json"
        try:
            artifact_id = self.files.store_artifact(
                data,
                relative_name=relative_name,
                stable_key=f"outcome-evaluation:{outcome_id}",
                user_scope_id=context.user_scope_id,
            )
        except ContractError:
            raise
        except OSError as error:
            raise ContractError(
                ErrorCode.STORAGE_COMMIT_FAILED,
                "the evaluation manifest could not be stored",
            ) from error
        evidence = [EvidenceRef(
            kind="artifact",
            file_id=artifact_id,
            content_hash=hashlib.sha256(data).hexdigest(),
            locator=relative_name,
        )]
        evidence.extend(
            EvidenceRef(kind="local", entity_type="Feedback", entity_id=item)
            for item in typed.feedback_ids
        )
        outcome = ExperimentOutcome(
            id=outcome_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            change_set_id=typed.change_set_id,
            base_applied_id=typed.base_applied_id,
            tested_applied_id=typed.tested_applied_id,
            before_bindings=list(typed.before_bindings),
            after_bindings=list(typed.after_bindings),
            comparison_id=typed.comparison_id,
            legacy_outcome=evaluation["legacy"],
            player_acceptance=typed.player_acceptance,
            effect_assessment=evaluation["effect"],
            feedback_ids=list(typed.feedback_ids),
            evidence_refs=evidence,
            limitations=evaluation["limitations"],
        )
        with self.repository.unit_of_work() as uow:
            winner = _replay_result(
                uow, context, OUTCOME_ROUTE, request_schema_version, request_hash,
            )
            if winner is not None:
                return winner
            uow.insert_object("ExperimentOutcome", outcome)
            uow.insert_reference(outcome.id, "change_set", ObjectRef(
                entity_type="ChangeSet", entity_id=outcome.change_set_id,
            ))
            uow.insert_reference(outcome.id, "base_applied", ObjectRef(
                entity_type="AppliedConfiguration",
                entity_id=outcome.base_applied_id,
            ))
            uow.insert_reference(outcome.id, "tested_applied", ObjectRef(
                entity_type="AppliedConfiguration",
                entity_id=outcome.tested_applied_id,
            ))
            if outcome.comparison_id is not None:
                uow.insert_reference(outcome.id, "comparison", ObjectRef(
                    entity_type="PerformanceComparison",
                    entity_id=outcome.comparison_id,
                ))
            for feedback_id in outcome.feedback_ids:
                uow.insert_reference(outcome.id, "feedback", ObjectRef(
                    entity_type="Feedback", entity_id=feedback_id,
                ))
            for summary in evaluation["summaries"]:
                uow.insert_reference(outcome.id, "summary", ObjectRef(
                    entity_type="PerformanceSummary", entity_id=summary.id,
                ))
            result = ResourceResult(refs=[
                ObjectRef(entity_type="ExperimentOutcome", entity_id=outcome.id)
            ])
            _record_result(
                uow, context, OUTCOME_ROUTE, request_schema_version,
                request_hash, result,
            )
            return result

    # -- reads -------------------------------------------------------------
    def get_outcome(self, outcome_id: str, user_scope_id: str) -> ExperimentOutcome:
        stored = self.repository.find_object("ExperimentOutcome", outcome_id)
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the outcome is not available"
            )
        outcome = ExperimentOutcome.model_validate_json(stored.payload_bytes)
        change_set = self.repository.find_object("ChangeSet", outcome.change_set_id)
        owner = (
            None if change_set is None
            else _change_set_owner(
                self.repository,
                ChangeSet.model_validate_json(change_set.payload_bytes),
            )
        )
        if owner != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the outcome is not available"
            )
        return outcome

    # -- verification ------------------------------------------------------
    def precheck(self, typed: OutcomeInput, user_scope_id: str) -> "_PreparedOutcome":
        change_set = _require_change_set(
            self.repository, typed.change_set_id, user_scope_id,
        )
        if typed.base_applied_id != change_set.base_applied_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the base does not equal the ChangeSet base",
                field_errors=[FieldError(
                    path="base_applied_id", code="reference",
                    message="differs from ChangeSet.base_applied_id",
                )],
            )
        base = _require_applied(self.repository, typed.base_applied_id, user_scope_id)
        tested = _require_applied(
            self.repository, typed.tested_applied_id, user_scope_id,
        )
        if base.installation_id != change_set.installation_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the base belongs to another installation",
            )
        if tested.installation_id != change_set.installation_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the tested configuration belongs to another installation",
            )
        extra_change = self._require_chain(change_set, tested, base)

        expected = {
            **{_binding_key(item): "before" for item in typed.before_bindings},
            **{_binding_key(item): "after" for item in typed.after_bindings},
        }
        if len(expected) != len(typed.before_bindings) + len(typed.after_bindings):
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "a binding is declared on both sides of the experiment",
            )
        limitations: list[str] = []
        for item in typed.before_bindings + typed.after_bindings:
            self.performance.require_analyzeable(item, user_scope_id)
            binding = self.performance.get_binding(
                item.segment_id, item.binding_revision, user_scope_id,
            )
            side = expected[_binding_key(item)]
            want = typed.base_applied_id if side == "before" else typed.tested_applied_id
            if binding.applied_id is None:
                limitations.append(
                    f"the {side} binding {item.segment_id} names no adopted"
                    " configuration"
                )
            elif binding.applied_id != want:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    f"the {side} binding does not name the declared configuration",
                    field_errors=[FieldError(
                        path=f"{side}_bindings", code="reference",
                        message="binding and declared configuration disagree",
                    )],
                )

        comparison = None
        protocol = None
        summaries: list[Any] = []
        if typed.comparison_id is not None:
            comparison = self.performance.get_comparison(
                typed.comparison_id, user_scope_id,
            )
            summaries = [
                self.performance.get_summary(item, user_scope_id)
                for item in comparison.summary_ids
            ]
            left_keys = {_binding_key(item) for item in summaries[0].bindings}
            right_keys = {_binding_key(item) for item in summaries[1].bindings}
            before_keys = {_binding_key(item) for item in typed.before_bindings}
            after_keys = {_binding_key(item) for item in typed.after_bindings}
            if not before_keys or not after_keys:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "a comparison needs bindings on both sides",
                )
            # 05 §7: the compared summaries' binding sets belong to the
            # declared before/after scope.  A reversed or mixed comparison
            # fails here; the declared inputs stay the reportable scope even
            # when they contain several repeated rounds.
            if not left_keys <= before_keys or not right_keys <= after_keys:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the comparison is reversed or mixes another experiment",
                    field_errors=[FieldError(
                        path="comparison_id", code="reference",
                        message="summary order and declared sides disagree",
                    )],
                )
            for summary in summaries:
                # A selected result whose frozen manifest is missing or corrupt
                # is not silently treated as an empty measurement.
                self.files.read_managed_file(summary.manifest_file_id)
            protocol = self.performance.get_comparison_protocol(
                comparison.protocol_id, user_scope_id,
            )
        for feedback_id in typed.feedback_ids:
            _require_feedback(self.repository, feedback_id, user_scope_id)
        return _PreparedOutcome(
            change_set=change_set, base=base, tested=tested,
            comparison=comparison, protocol=protocol, summaries=summaries,
            extra_change=extra_change, limitations=limitations,
        )

    def _require_chain(
        self, change_set: ChangeSet, tested: AppliedConfiguration,
        base: AppliedConfiguration,
    ) -> bool:
        """Walk ``parent_applied_id``; any node beyond the ChangeSet is extra."""
        current = tested
        seen: set[str] = set()
        found = False
        extra = False
        while current is not None:
            if current.id in seen:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the adopted configuration chain contains a cycle",
                )
            seen.add(current.id)
            if current.change_set_id == change_set.id:
                found = True
                if current.parent_applied_id != base.id:
                    # The change was applied on another baseline, or the
                    # player edited something else first.
                    extra = True
                if current.restored_from_applied_id is not None:
                    extra = True
                break
            if current.id == base.id:
                break
            if current.parent_applied_id is None:
                break
            if current.origin.value in {"manual_edit", "change_set", "rollback"}:
                extra = True
            current = _require_applied(
                self.repository, current.parent_applied_id,
                current.user_scope_id,
            )
        if not found:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the tested configuration does not contain this ChangeSet",
                field_errors=[FieldError(
                    path="tested_applied_id", code="reference",
                    message="the adopted chain has no such change",
                )],
            )
        return extra

    # -- reduction ---------------------------------------------------------
    def _evaluate(
        self, typed: OutcomeInput, prepared: "_PreparedOutcome", *,
        outcome_id: str, now: str,
    ) -> dict[str, Any]:
        limitations = list(prepared.limitations)
        checks = [
            _outcome_check(
                "outcome.chain", CheckStatus.pass_, now=now,
                note="the tested configuration contains this ChangeSet",
            ),
            _outcome_check(
                "outcome.base", CheckStatus.pass_, now=now,
                note="the base equals the ChangeSet base and the frozen snapshot",
            ),
        ]
        before_rounds: list[Any] = []
        after_rounds: list[Any] = []
        conflict = False
        if typed.comparison_id is not None:
            before_rounds = self.performance.summaries_for(
                typed.before_bindings, prepared.base.user_scope_id,
            )
            after_rounds = self.performance.summaries_for(
                typed.after_bindings, prepared.base.user_scope_id,
            )
            for summary in before_rounds + after_rounds:
                self.files.read_managed_file(summary.manifest_file_id)
            conflict = _has_numeric_conflict(before_rounds) or _has_numeric_conflict(
                after_rounds
            )
            checks.append(_outcome_check(
                "outcome.rounds", CheckStatus.pass_, now=now,
                note=f"repeated samples: {len(before_rounds)} before,"
                     f" {len(after_rounds)} after",
            ))
        rounds: list[_MetricRound] = []
        comparison_ok = True
        if prepared.comparison is not None:
            comparison_ok = (
                all(
                    item.status in {CheckStatus.pass_, CheckStatus.not_applicable}
                    for item in prepared.comparison.checks
                )
            )
            checks.append(_outcome_check(
                "outcome.conditions", CheckStatus.pass_ if comparison_ok else (
                    CheckStatus.unknown
                ),
                now=now,
                note="the frozen comparison reports its condition checks"
                     if comparison_ok else
                     "a condition check failed or is unknown in the comparison",
            ))
            rounds = self._metric_rounds(
                prepared, before_rounds, after_rounds, limitations,
            )
        else:
            limitations.append("no comparison was supplied; the effect is unmeasured")
            checks.append(_outcome_check(
                "outcome.measurement", CheckStatus.not_run, now=now,
                note="no comparison or repeated measurement was supplied",
            ))

        effect = self._effect(
            typed, prepared, rounds, conflict=conflict,
            comparison_ok=comparison_ok, limitations=limitations, now=now,
            before_count=len(before_rounds), after_count=len(after_rounds),
        )
        legacy = _legacy_reduction(typed.player_acceptance, effect)
        manifest = {
            "kind": "outcome_evaluation",
            "rule_version": OUTCOME_RULE_VERSION,
            "effect_policy_version": EFFECT_POLICY_VERSION,
            "outcome_id": outcome_id,
            "change_set_id": typed.change_set_id,
            "base_applied_id": typed.base_applied_id,
            "tested_applied_id": typed.tested_applied_id,
            "comparison_id": typed.comparison_id,
            "protocol_id": (
                None if prepared.comparison is None
                else prepared.comparison.protocol_id
            ),
            "exploratory": (
                None if prepared.protocol is None else prepared.protocol.exploratory
            ),
            "before_bindings": [
                item.model_dump(mode="json") for item in typed.before_bindings
            ],
            "after_bindings": [
                item.model_dump(mode="json") for item in typed.after_bindings
            ],
            "selected_summary_ids": [
                item.id for item in prepared.summaries
            ],
            "before_summary_ids": [item.id for item in before_rounds],
            "after_summary_ids": [item.id for item in after_rounds],
            "round_counts": {
                "before": len(before_rounds), "after": len(after_rounds),
            },
            "extra_player_change": prepared.extra_change,
            "metrics": [round_.__dict__ for round_ in rounds],
            "primary_definition_id": (
                None if not rounds else rounds[0].definition_id
            ),
            "condition_checks": (
                [] if prepared.comparison is None else [
                    item.model_dump(mode="json")
                    for item in prepared.comparison.checks
                ]
            ),
            "limitations": list(limitations),
            "effect_assessment": effect.value,
            "legacy_outcome": legacy.value,
            "player_acceptance": typed.player_acceptance.value,
            # E-R23: the rules are proven offline; no real local effect is claimed.
            "local_effect": "not_run",
            "evidence_level": "offline_fixture",
        }
        manifest["checks"] = [item.model_dump(mode="json") for item in checks]
        return {
            "effect": effect, "legacy": legacy, "limitations": limitations,
            "manifest": manifest,
            "summaries": prepared.summaries,
        }

    def _metric_rounds(
        self, prepared: "_PreparedOutcome", before_rounds: Sequence[Any],
        after_rounds: Sequence[Any], limitations: list[str],
    ) -> list[_MetricRound]:
        if prepared.comparison is None or not before_rounds or not after_rounds:
            return []
        selected = prepared.summaries
        if not selected:
            return []
        analyzer = selected[0].analyzer_version
        cleaning = selected[0].cleaning_protocol_version
        before = [
            item for item in before_rounds
            if item.analyzer_version == analyzer
            and item.cleaning_protocol_version == cleaning
        ]
        after = [
            item for item in after_rounds
            if item.analyzer_version == analyzer
            and item.cleaning_protocol_version == cleaning
        ]
        if len(before) != len(before_rounds) or len(after) != len(after_rounds):
            limitations.append(
                "some repeated samples used another analyzer/cleaning rule set"
                " and were left out"
            )
        usable = {
            item.metric_definition_id
            for item in prepared.comparison.per_metric
            if item.status is CheckStatus.pass_
        }
        before_values = _values_by_definition(before)
        after_values = _values_by_definition(after)
        definitions = self._definitions(set(before_values) & set(after_values) & usable)
        rounds: list[_MetricRound] = []
        for definition_id, definition in definitions.items():
            key = definition.get("metric_key")
            direction = _metric_direction(key)
            if direction is None:
                continue
            before_numbers = before_values[definition_id]
            after_numbers = after_values[definition_id]
            if len(before_numbers) != len(before) or len(after_numbers) != len(after):
                limitations.append(
                    f"metric {key} has no numeric value in every round"
                )
                continue
            before_median = float(median(before_numbers))
            after_median = float(median(after_numbers))
            before_range = max(before_numbers) - min(before_numbers)
            after_range = max(after_numbers) - min(after_numbers)
            delta = after_median - before_median
            tolerance = max(
                1e-9,
                1e-9 * max(abs(before_median), abs(after_median), 1.0),
            )
            rounds.append(_MetricRound(
                definition_id=definition_id,
                metric_key=key,
                direction=direction,
                before_median=before_median,
                after_median=after_median,
                before_range=before_range,
                after_range=after_range,
                delta=delta,
                beyond_fluctuation=(
                    abs(delta) > max(before_range, after_range) + tolerance
                ),
            ))
        priority = {key: index for index, key in enumerate(_PRIMARY_ORDER)}
        rounds.sort(key=lambda item: (
            priority.get(item.metric_key, len(priority)), item.metric_key,
        ))
        return rounds

    def _definitions(self, definition_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        with self.repository.unit_of_work() as uow:
            for definition_id in definition_ids:
                stored = uow.find_object("MetricDefinition", definition_id)
                if stored is not None:
                    result[definition_id] = json.loads(stored.payload_bytes)
        return result

    def _effect(
        self, typed: OutcomeInput, prepared: "_PreparedOutcome",
        rounds: Sequence[_MetricRound], *, conflict: bool, comparison_ok: bool,
        limitations: list[str], now: str, before_count: int, after_count: int,
    ) -> EffectAssessment:
        if typed.comparison_id is None:
            return EffectAssessment.not_measured
        if prepared.protocol is not None and prepared.protocol.exploratory:
            limitations.append(
                "exploratory comparison: observed differences are not reduced"
                " to a stable improvement or regression"
            )
            return EffectAssessment.inconclusive
        if prepared.comparison is not None and prepared.comparison.level.value != "controlled":
            limitations.append(
                "the comparison is not controlled; its difference is descriptive"
            )
            return EffectAssessment.inconclusive
        if not comparison_ok:
            return EffectAssessment.inconclusive
        if prepared.extra_change:
            limitations.append(
                "the adopted chain contains a player change outside this ChangeSet;"
                " the result is downgraded"
            )
            return EffectAssessment.inconclusive
        if conflict:
            limitations.append(
                "two summaries for one binding disagree; the conflict is not"
                " resolved by picking the better number"
            )
            return EffectAssessment.inconclusive
        if not rounds:
            limitations.append("no metric was comparable across the frozen protocol")
            return EffectAssessment.inconclusive
        if min(before_count, after_count) < _MIN_ROUNDS:
            limitations.append(
                f"fewer than {_MIN_ROUNDS} valid rounds per side; direction is not"
                " reduced from a single or double sample"
            )
            return EffectAssessment.inconclusive
        primary = rounds[0]
        def _improves(item: _MetricRound) -> bool:
            if not item.beyond_fluctuation:
                return False
            return (
                item.delta > 0
                if item.direction == "higher_better"
                else item.delta < 0
            )

        def _worsens(item: _MetricRound) -> bool:
            if not item.beyond_fluctuation:
                return False
            return (
                item.delta < 0
                if item.direction == "higher_better"
                else item.delta > 0
            )

        if not primary.beyond_fluctuation:
            if any(item.beyond_fluctuation for item in rounds):
                limitations.append(
                    "the primary metric is within fluctuation while another"
                    " relevant metric changed"
                )
                return EffectAssessment.inconclusive
            return EffectAssessment.no_clear_change
        others = rounds[1:]
        if _improves(primary):
            if any(_worsens(item) for item in others):
                limitations.append(
                    "the primary metric improves while another relevant metric"
                    " regresses; the tradeoff is unresolved"
                )
                return EffectAssessment.inconclusive
            return EffectAssessment.improved
        if _worsens(primary):
            if any(_improves(item) for item in others):
                limitations.append(
                    "the primary metric regresses while another improves; the"
                    " tradeoff is unresolved"
                )
                return EffectAssessment.inconclusive
            return EffectAssessment.worse
        return EffectAssessment.inconclusive


@dataclass(frozen=True)
class _PreparedOutcome:
    change_set: ChangeSet
    base: AppliedConfiguration
    tested: AppliedConfiguration
    comparison: Any | None
    protocol: Any | None
    summaries: list[Any]
    extra_change: bool
    limitations: list[str]


def _values_by_definition(summaries: Sequence[Any]) -> dict[str, list[float]]:
    values: dict[str, list[float]] = {}
    for summary in summaries:
        for metric in summary.metrics:
            numeric = metric.numeric
            if numeric.number_kind in {"exact", "approximate"} and numeric.value is not None:
                values.setdefault(metric.definition_id, []).append(float(numeric.value))
    return values


def _has_numeric_conflict(summaries: Sequence[Any]) -> bool:
    groups: dict[tuple, list[dict[str, float | None]]] = {}
    for summary in summaries:
        key = (
            tuple(sorted(_binding_key(item) for item in summary.bindings)),
            summary.analyzer_version,
            summary.cleaning_protocol_version,
        )
        values = {
            metric.definition_id: (
                metric.numeric.value
                if metric.numeric.number_kind in {"exact", "approximate"}
                else None
            )
            for metric in summary.metrics
        }
        groups.setdefault(key, []).append(values)
    for values in groups.values():
        if len(values) < 2:
            continue
        first = values[0]
        for other in values[1:]:
            if first != other:
                return True
    return False


def _metric_direction(metric_key: str | None) -> str | None:
    if metric_key in _HIGHER_IS_BETTER:
        return "higher_better"
    if metric_key in _LOWER_IS_BETTER:
        return "lower_better"
    return None


def _replay_result(
    uow: UnitOfWork, context: MutationContext, route: str,
    request_schema_version: str, request_hash: str,
) -> ResourceResult | None:
    existing = uow.find_idempotency(
        context.user_scope_id, "POST", route, context.idempotency_key,
    )
    if existing is None:
        return None
    if existing.lifecycle.value == "tombstoned":
        raise ContractError(
            ErrorCode.RESOURCE_GONE,
            "this submission was cleaned up; start a new logical submission",
        )
    if existing.request_schema_version != request_schema_version:
        raise ContractError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "this key is registered under another contract version",
        )
    if existing.request_hash != request_hash:
        raise ContractError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "the same key was already used with different business content",
        )
    return ResourceResult.model_validate(json.loads(existing.response_body))


def _record_result(
    uow: UnitOfWork, context: MutationContext, route: str,
    request_schema_version: str, request_hash: str, result: ResourceResult,
) -> None:
    uow.insert_idempotency(
        user_scope_id=context.user_scope_id,
        method="POST",
        canonical_route=route,
        key=context.idempotency_key,
        request_schema_version=request_schema_version,
        request_hash=request_hash,
        response_status=201,
        response_body=canonical_json(result.model_dump(mode="json")),
        result_refs=[ref.model_dump(mode="json") for ref in result.refs],
    )


class _NextActionProtocolError(ValueError):
    """The provider's hypotheses reply has no readable shape."""


def _parse_hypotheses(reply: Any, *, task_id: str) -> list[Hypothesis]:
    """Read only hypotheses/counter/test_action; every other key is ignored."""
    if not isinstance(reply, Mapping):
        raise _NextActionProtocolError("the reply is not an object")
    items = reply.get("hypotheses", [])
    if not isinstance(items, list):
        raise _NextActionProtocolError("hypotheses is not a list")
    parsed: list[Hypothesis] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        statement = item.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            continue
        unknowns = [
            value for value in (item.get("unknowns") or [])
            if isinstance(value, str)
        ]
        test_action = item.get("test_action")
        parsed.append(Hypothesis(
            id=_stable_id("hypothesis", task_id, "model", index, statement),
            statement=statement,
            support=_read_evidence(item.get("support")),
            counter=_read_evidence(item.get("counter")),
            unknowns=unknowns,
            test_action=test_action if isinstance(test_action, str) else "",
        ))
    return parsed


def _read_evidence(value: Any) -> list[EvidenceRef]:
    if not isinstance(value, list):
        return []
    result: list[EvidenceRef] = []
    for item in value:
        try:
            result.append(EvidenceRef.model_validate(item))
        except Exception:  # noqa: BLE001 - an unreadable hint is diagnostic only
            continue
    return result


def _latest_feedback(feedbacks: Sequence[Feedback]) -> Feedback | None:
    if not feedbacks:
        return None
    return max(feedbacks, key=lambda item: (item.created_at, item.id))


@dataclass(frozen=True)
class _CandidateAction:
    action: Any
    old_value: FieldValue[Any]


@dataclass(frozen=True)
class _Candidate:
    actions: list[_CandidateAction]
    checks: list[CheckResult]
    gaps: list[str]
    evidence_refs: list[EvidenceRef]
    protected_preference_ids: list[str]


@dataclass(frozen=True)
class _Decision:
    action: NextActionKind
    statement: str
    unknowns: list[str]
    test_action: str
    checks: list[CheckResult]
    change_set: ChangeSet | None = None


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
