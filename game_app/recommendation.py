"""Deterministic recommendation verification (REC-T01 / REC-T02).

The model may propose candidates and explain them; this module decides the pool,
the purpose-match class, the actions and the readiness with ordinary code.  No
function here calls a provider, reads an image, predicts performance or runs a
model-supplied expression.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    KnowledgeRecordRef,
    ObjectRef,
)
from game_app.contracts.device import DeviceSnapshot, GameContext
from game_app.contracts.enums import (
    CheckStatus,
    GameIdentityStatus,
    LegacyRecordState,
    PublicationStatus,
    ValueState,
)
from game_app.contracts.knowledge import (
    Game,
    ImportedRecord,
    KnowledgePackage,
    SettingIdentityMapping,
)
from game_app.contracts.primitives import utc_now
from game_app.contracts.recommendation import (
    AssignmentV01,
    CandidateAssessment,
    SettingValueV01,
)
from game_app.contracts.tasks import RecommendationInput
from game_app.errors import ContractError, ErrorCode, FieldError
from game_app.repository import UnitOfWork, canonical_json

CHECK_DEFINITION = "recommendation-checks/0.4"
# Only declarative equality over these facts is executable.  Any other key is a
# question, not a rule the product may guess at.
EXECUTABLE_FACTS = frozenset({
    "platform", "gpu", "resolution", "device_form", "game_version",
})
_COMPARISON_MARKERS = (">", "<", "~", "^", "*", "!=")
_ACCEPTED_CONFIGURATION_STATUS = {"candidate", "needs_followup", "excluded_branch"}
_POSITIVE_APPLICABILITY = frozenset({"applicable", "pass", "verified"})
_VISUAL_EVIDENCE_KINDS = frozenset({
    "observation", "recommendation", "setting_description",
})


# --- small helpers ----------------------------------------------------------


def _check(
    check_id: str,
    status: CheckStatus,
    *,
    clock=utc_now,
    method: str = "deterministic recommendation rule",
    note: str | None = None,
    evidence_refs: Sequence[EvidenceRef] = (),
    input_refs: Sequence[ObjectRef] = (),
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=CHECK_DEFINITION,
        status=status,
        input_refs=list(input_refs),
        evidence_refs=list(evidence_refs),
        checked_at=(
            None if status in (CheckStatus.not_run, CheckStatus.unknown,
                               CheckStatus.not_applicable)
            else clock()
        ),
        method=method,
        note=note,
    )


def _field(source: Any, name: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _normalise(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None


def _normalise_resolution(value: Any) -> str | None:
    text = _normalise(value)
    if text is None:
        return None
    for marker in ("×", " ", "*"):
        text = text.replace(marker, "x")
    return text


def _known(value: Any) -> bool:
    return getattr(value, "value_state", None) is ValueState.known


def _value_of(value: Any) -> Any:
    return getattr(value, "value", None) if _known(value) else None


def _record_ref(package_id: str, record: ImportedRecord) -> KnowledgeRecordRef:
    return KnowledgeRecordRef(
        package_id=package_id,
        record_snapshot_id=record.id,
        source_revision_id=record.source_revision_id,
        capture_scope_id=record.capture_scope_id,
        local_record_id=record.local_record_id,
        record_type=record.record_type,
        payload_hash=record.payload_hash,
    )


def _knowledge_evidence(package_id: str, record: ImportedRecord) -> EvidenceRef:
    return EvidenceRef(kind="knowledge", record=_record_ref(package_id, record))


def _facts_from_device(
    device: DeviceSnapshot, context: GameContext
) -> dict[str, str | None]:
    gpu_name = None
    if device.selected_gpu_id is not None:
        for gpu in device.gpus:
            if gpu.id == device.selected_gpu_id:
                gpu_name = _normalise(_value_of(gpu.raw_name))
                break
    resolution = None
    desktop = device.display.desktop_resolution
    if _known(desktop):
        resolution = _normalise_resolution(
            f"{desktop.value.width}x{desktop.value.height}"
        )
    version = None
    if context.build_identifiers:
        version = _normalise(context.build_identifiers[0].raw_value)
    return {
        "platform": _normalise(_value_of(context.platform)),
        "gpu": gpu_name,
        "resolution": resolution,
        "device_form": _normalise(_value_of(device.device_form)),
        "game_version": version,
    }


# --- input gate -------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    task_input: RecommendationInput
    package: KnowledgePackage
    context: GameContext
    game: Game
    device: DeviceSnapshot
    applied: Any | None
    summaries: list[Any]
    facts: dict[str, str | None]
    limitations: list[str]
    checks: list[CheckResult]
    user_scope_id: str


class InputGate:
    """Resolve and verify every input without writing anything (03 §4/§9)."""

    def __init__(
        self,
        repository: Any,
        knowledge: Any,
        adoption: Any | None = None,
        performance: Any | None = None,
        *,
        clock=utc_now,
    ) -> None:
        self.repository = repository
        self.knowledge = knowledge
        self.adoption = adoption
        self.performance = performance
        self.clock = clock

    def check(
        self, task_input: RecommendationInput, *, user_scope_id: str
    ) -> GateResult:
        if task_input.preference_ids:
            # 05 has no Preference contract; a non-empty list cannot be honoured.
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "no explicit preference is visible in this product build",
                field_errors=[FieldError(
                    path="preference_ids", code="reference",
                    message="preferences are not implemented (05)",
                )],
            )
        package = self.knowledge.get_package(
            task_input.package_id, user_scope_id=user_scope_id, verify_artifacts=True,
        )
        if package.publication_status is not PublicationStatus.published:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "an unpublished package is not a recommendation input",
            )
        context = self.knowledge.resolve_context(
            task_input.game_context_id, user_scope_id
        )
        game = self.knowledge.resolve_game(context, user_scope_id)
        if package.game_id != context.game_id or game.series_id != context.game_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the package, context and game do not name one game",
            )

        stored = self.repository.find_object(
            "DeviceSnapshot", task_input.device_snapshot_id
        )
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "the requested device snapshot is not available",
            )
        device = DeviceSnapshot.model_validate_json(stored.payload_bytes)
        if device.user_scope_id != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "the requested device snapshot is not available",
            )

        limitations: list[str] = []
        if game.identity_status is not GameIdentityStatus.verified:
            limitations.append(
                "the game identity is not verified; no recommendation can be"
                " actionable"
            )

        applied = None
        if task_input.applied_id is not None:
            applied = self._applied(
                task_input.applied_id, context, device, limitations, user_scope_id,
            )
        summaries = [
            self._summary(summary_id, context, user_scope_id)
            for summary_id in task_input.history_summary_ids
        ]

        checks = [
            _check("identity.context", CheckStatus.pass_, clock=self.clock),
            _check(
                "identity.game",
                CheckStatus.pass_
                if game.identity_status is GameIdentityStatus.verified
                else CheckStatus.unknown,
                clock=self.clock,
                note=game.identity_status.value,
            ),
            _check("identity.package", CheckStatus.pass_, clock=self.clock),
            _check("identity.device", CheckStatus.pass_, clock=self.clock),
            _check(
                "reference.applied",
                CheckStatus.pass_ if applied is not None else CheckStatus.not_applicable,
                clock=self.clock,
            ),
            _check(
                "reference.summary",
                CheckStatus.pass_ if summaries else CheckStatus.not_applicable,
                clock=self.clock,
            ),
            _check(
                "preference.explicit",
                CheckStatus.not_applicable,
                clock=self.clock,
                note="no preference contract exists in this build (05)",
            ),
        ]
        return GateResult(
            task_input=task_input,
            package=package,
            context=context,
            game=game,
            device=device,
            applied=applied,
            summaries=summaries,
            facts=_facts_from_device(device, context),
            limitations=limitations,
            checks=checks,
            user_scope_id=user_scope_id,
        )

    # -- sub-resources -----------------------------------------------------
    def _applied(
        self,
        applied_id: str,
        context: GameContext,
        device: DeviceSnapshot,
        limitations: list[str],
        user_scope_id: str,
    ) -> Any:
        if self.adoption is None:
            # Imported here to avoid a module-level cycle with the adoption model.
            from game_app.adoption import AdoptionService

            self.adoption = AdoptionService(self.repository, self.knowledge.files)
        applied = self.adoption.get_applied(applied_id)
        if getattr(applied, "user_scope_id", None) != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "the requested applied configuration is not available",
            )
        if applied.game_id != context.game_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the applied configuration belongs to another game",
            )
        stored = self.repository.find_object(
            "DeviceSnapshot", applied.device_snapshot_id
        )
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the applied configuration names a device snapshot that is absent",
            )
        applied_device = DeviceSnapshot.model_validate_json(stored.payload_bytes)
        if applied_device.device_series_id != device.device_series_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the applied configuration belongs to another device series",
            )
        if applied.device_snapshot_id != device.id:
            limitations.append(
                "the current values come from an older device snapshot in the"
                " same device series"
            )
        return applied

    def _summary(
        self, summary_id: str, context: GameContext, user_scope_id: str
    ) -> Any:
        if self.performance is None:
            from game_app.performance import PerformanceService

            self.performance = PerformanceService(
                self.repository, self.knowledge.files
            )
        summary = self.performance.get_summary(summary_id, user_scope_id)
        with self.repository.unit_of_work() as uow:
            game_id = self._summary_game_id(uow, summary)
        if game_id is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the history summary's game cannot be proven; it is not used",
            )
        if game_id != context.game_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the history summary belongs to another game",
            )
        return summary

    @staticmethod
    def _summary_game_id(uow: UnitOfWork, summary: Any) -> str | None:
        stored = uow.find_object("CaptureArtifact", summary.artifact_id)
        if stored is None:
            return None
        artifact = stored.payload
        session_stored = uow.find_object("PlaySession", artifact["session_id"])
        if session_stored is None:
            return None
        session = session_stored.payload
        context_stored = uow.find_object("GameContext", session["game_context_id"])
        if context_stored is None:
            return None
        return context_stored.payload.get("game_id")


# --- declarative constraints ------------------------------------------------


@dataclass(frozen=True)
class ConstraintExclusion:
    constraint_id: str
    setting_id: str
    values: frozenset[str]
    evidence_ref: EvidenceRef | None


@dataclass(frozen=True)
class ConstraintEvaluation:
    exclusions: list[ConstraintExclusion]
    checks: list[CheckResult]
    limitations: list[str] = field(default_factory=list)


class ConstraintEvaluator:
    """Execute only verified declarative equality; everything else is unknown.

    A numeric threshold, a strict boundary, a complex version predicate or an
    unknown fact key is never evaluated; the constraint is reported and left as
    a follow-up question.  No expression from a source or a model is executed.
    """

    def __init__(self, *, clock=utc_now) -> None:
        self.clock = clock

    def evaluate(
        self, records: Iterable[ImportedRecord], facts: Mapping[str, str | None]
    ) -> ConstraintEvaluation:
        exclusions: list[ConstraintExclusion] = []
        checks: list[CheckResult] = []
        limitations: list[str] = []
        for record in records:
            if record.record_type != "constraints":
                continue
            if record.upstream_status is not LegacyRecordState.accepted:
                continue
            outcome, exclusion, limitation = self._evaluate_one(record, facts)
            checks.append(outcome)
            if exclusion is not None:
                exclusions.append(exclusion)
            if limitation is not None:
                limitations.append(limitation)
        return ConstraintEvaluation(
            exclusions=exclusions, checks=checks, limitations=limitations,
        )

    def _evaluate_one(
        self, record: ImportedRecord, facts: Mapping[str, str | None]
    ) -> tuple[CheckResult, ConstraintExclusion | None, str | None]:
        payload = record.payload if isinstance(record.payload, Mapping) else {}
        check_id = "value.constraint"
        kind = payload.get("kind")
        when = payload.get("when")
        requires = payload.get("requires") or {}
        setting_id = payload.get("setting_id")
        affected = payload.get("affected_values") or []
        verified = payload.get("verified_scope") is True
        reason = self._non_executable_reason(kind, when, requires, affected)
        if reason is None and not verified:
            reason = "the source does not prove the constraint's scope"
        if reason is None and not isinstance(setting_id, str):
            reason = "the constraint names no setting"
        if reason is not None:
            return (
                _check(
                    check_id, CheckStatus.unknown, clock=self.clock,
                    note=reason, evidence_refs=[_knowledge_evidence(
                        record.import_snapshot_id, record)],
                ),
                None,
                f"constraint {record.local_record_id} is not executable: {reason}",
            )

        match = self._conditions_hold(when, facts)
        if match is None:
            return (
                _check(
                    check_id, CheckStatus.unknown, clock=self.clock,
                    note="a condition's fact is unknown in this context",
                ),
                None,
                f"constraint {record.local_record_id} depends on an unknown fact",
            )
        if not match:
            return (
                _check(check_id, CheckStatus.pass_, clock=self.clock,
                       note="the constraint does not apply to this context"),
                None,
                None,
            )
        requires_match = self._conditions_hold(requires, facts) if requires else True
        if requires_match is None:
            return (
                _check(
                    check_id, CheckStatus.unknown, clock=self.clock,
                    note="a required fact is unknown in this context",
                ),
                None,
                f"constraint {record.local_record_id} requires an unknown fact",
            )
        if kind == "exclusion":
            values = frozenset(str(item) for item in affected)
            return (
                _check(check_id, CheckStatus.pass_, clock=self.clock,
                       note="the exclusion applies"),
                ConstraintExclusion(
                    constraint_id=record.id,
                    setting_id=setting_id,
                    values=values,
                    evidence_ref=_knowledge_evidence(
                        record.import_snapshot_id, record
                    ),
                ),
                None,
            )
        if kind == "dependency" and not requires_match:
            values = frozenset(str(item) for item in affected)
            return (
                _check(check_id, CheckStatus.pass_, clock=self.clock,
                       note="the dependency is not met"),
                ConstraintExclusion(
                    constraint_id=record.id,
                    setting_id=setting_id,
                    values=values,
                    evidence_ref=_knowledge_evidence(
                        record.import_snapshot_id, record
                    ),
                ),
                None,
            )
        return (
            _check(check_id, CheckStatus.pass_, clock=self.clock,
                   note="the declarative rule is satisfiable"),
            None,
            None,
        )

    @staticmethod
    def _non_executable_reason(kind, when, requires, affected) -> str | None:
        if kind not in ("exclusion", "support", "dependency"):
            return "the constraint kind is not executed deterministically"
        if not isinstance(when, Mapping) or not when:
            return "the constraint has no declarative condition"
        if not isinstance(requires, Mapping):
            return "the requirement is not a declarative mapping"
        if not isinstance(affected, list):
            return "the affected values are not a list"
        for key, value in list(when.items()) + list(requires.items()):
            if not isinstance(key, str) or key not in EXECUTABLE_FACTS:
                return f"the condition key {key!r} has no verified executor"
            if isinstance(value, str) and any(
                marker in value for marker in _COMPARISON_MARKERS
            ):
                return "a comparison expression is not a declarative equality"
            if isinstance(value, (dict, list)):
                return "a structured predicate is not a declarative equality"
        return None

    @staticmethod
    def _conditions_hold(
        conditions: Mapping[str, Any], facts: Mapping[str, str | None]
    ) -> bool | None:
        for key, expected in conditions.items():
            actual = facts.get(key)
            if actual is None:
                return None
            if isinstance(expected, str):
                if _normalise(expected) != actual:
                    return False
            else:
                return None
        return True


# --- candidates -------------------------------------------------------------


@dataclass(frozen=True)
class CandidateBuildResult:
    assessments: list[CandidateAssessment]
    diagnostics: list[str]
    checks: list[CheckResult]


class CandidateBuilder:
    """Build the candidate pool from package records, never from the model."""

    def __init__(self, repository: Any, knowledge: Any, *, clock=utc_now) -> None:
        self.repository = repository
        self.knowledge = knowledge
        self.clock = clock
        self.constraints = ConstraintEvaluator(clock=clock)

    def build(
        self,
        gate: GateResult,
        *,
        model_candidates: Sequence[Any] | None = None,
    ) -> CandidateBuildResult:
        with self.repository.unit_of_work() as uow:
            records = self.knowledge._package_records(uow, gate.package)
        by_local: dict[tuple[str, str], ImportedRecord] = {}
        for record in records:
            by_local.setdefault(
                (record.capture_scope_id, record.local_record_id), record
            )
        source_facts = self._source_facts(uow, records)
        evaluation = self.constraints.evaluate(records, gate.facts)
        checks: list[CheckResult] = list(evaluation.checks)
        diagnostics: list[str] = list(evaluation.limitations)
        assessments: list[CandidateAssessment] = []
        for record in records:
            if record.record_type != "configurations":
                continue
            if record.upstream_status is not LegacyRecordState.accepted:
                continue
            payload = record.payload if isinstance(record.payload, Mapping) else {}
            if payload.get("record_status") not in (None, "accepted"):
                continue
            envelope = record.upstream_envelope or {}
            status = envelope.get("recommendation_status")
            applicability = envelope.get("applicability")
            if status == "excluded_branch" or applicability == "fail":
                continue
            assignments = self._assignments(payload)
            if assignments is None:
                diagnostics.append(
                    f"configuration {record.local_record_id} has no readable"
                    " assignment shape and is not a candidate"
                )
                continue
            environment = self._environment(
                payload, by_local, record.capture_scope_id
            )
            if self._platform_mismatch(environment, gate.facts):
                diagnostics.append(
                    f"configuration {record.local_record_id} is proven to be for"
                    " another platform and leaves the pool"
                )
                continue
            if self._excluded(assignments, evaluation.exclusions):
                diagnostics.append(
                    f"configuration {record.local_record_id} is filtered by a"
                    " verified declarative constraint"
                )
                continue
            assessments.append(self._assessment(
                gate, record, payload, assignments, environment, status,
                applicability, by_local, source_facts,
            ))

        self._screen_model_candidates(
            model_candidates or [], records, diagnostics,
        )
        assessments.sort(
            key=lambda item: item.configuration_ref.record_snapshot_id
        )
        return CandidateBuildResult(
            assessments=assessments,
            diagnostics=diagnostics,
            checks=checks,
        )

    # -- parsing -----------------------------------------------------------
    @staticmethod
    def _assignments(payload: Mapping[str, Any]) -> list[AssignmentV01] | None:
        raw = payload.get("assignments")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            return None
        assignments: list[AssignmentV01] = []
        for item in raw:
            if not isinstance(item, Mapping):
                return None
            try:
                assignments.append(AssignmentV01.model_validate(item))
            except ValueError:
                return None
        if not assignments and not payload.get("preset_reference"):
            return None
        return assignments

    @staticmethod
    def _environment(
        payload: Mapping[str, Any],
        by_local: Mapping[tuple[str, str], ImportedRecord],
        scope: str,
    ) -> ImportedRecord | None:
        for env_id in payload.get("environment_ids") or []:
            record = by_local.get((scope, str(env_id)))
            if record is not None and record.record_type == "environments":
                return record
        return None

    @staticmethod
    def _environment_facts(record: ImportedRecord | None) -> dict[str, Any]:
        if record is None or not isinstance(record.payload, Mapping):
            return {}
        fields = record.payload.get("fields")
        if not isinstance(fields, Mapping):
            return {}
        facts: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, Mapping):
                facts[key] = value.get("value")
            else:
                facts[key] = value
        return facts

    @staticmethod
    def _platform_mismatch(
        environment: ImportedRecord | None, facts: Mapping[str, str | None]
    ) -> bool:
        env = CandidateBuilder._environment_facts(environment)
        env_platform = _normalise(env.get("platform"))
        ctx_platform = facts.get("platform")
        return bool(env_platform and ctx_platform and env_platform != ctx_platform)

    @staticmethod
    def _excluded(
        assignments: Sequence[AssignmentV01],
        exclusions: Sequence[ConstraintExclusion],
    ) -> bool:
        for exclusion in exclusions:
            for assignment in assignments:
                if assignment.setting_id != exclusion.setting_id:
                    continue
                if not exclusion.values:
                    return True
                candidates = (
                    [assignment.value.value]
                    if assignment.value.value_kind == "fixed"
                    else list(assignment.value.alternatives)
                )
                if any(str(item) in exclusion.values for item in candidates):
                    return True
        return False

    # -- assessment --------------------------------------------------------
    def _assessment(
        self,
        gate: GateResult,
        record: ImportedRecord,
        payload: Mapping[str, Any],
        assignments: Sequence[AssignmentV01],
        environment: ImportedRecord | None,
        status: Any,
        applicability: Any,
        by_local: Mapping[tuple[str, str], ImportedRecord],
        source_facts: Mapping[str, tuple[str | None, str | None, bool]],
    ) -> CandidateAssessment:
        env = self._environment_facts(environment)
        facts = gate.facts
        conditions: list[CheckResult] = []
        limitations: list[str] = list(gate.limitations)

        platform = self._compare(
            "candidate.platform", facts.get("platform"),
            _normalise(env.get("platform")),
        )
        gpu = self._compare("candidate.gpu", facts.get("gpu"),
                            _normalise(env.get("gpu")))
        resolution = self._compare(
            "candidate.resolution", facts.get("resolution"),
            _normalise_resolution(env.get("resolution")),
        )
        version = self._compare(
            "candidate.version", facts.get("game_version"),
            _normalise(env.get("game_version") or env.get("version")),
        )
        reproducibility, unresolved = self._reproducibility(
            payload, assignments, by_local, record.capture_scope_id,
        )
        applicability_check = self._applicability(applicability)
        status_check = _check(
            "candidate.pool",
            CheckStatus.pass_
            if status == "candidate"
            else CheckStatus.not_run,
            clock=self.clock,
            note=f"recommendation_status={status!r}",
        )

        performance_basis, performance_records = self._basis(
            gate.package, assignments, by_local, record.capture_scope_id,
            "performance",
        )
        visual_basis, visual_records = self._basis(
            gate.package, assignments, by_local, record.capture_scope_id,
            "visual",
        )
        independence = self._independence(
            gate.package, list(performance_records) + list(visual_records),
            source_facts,
        )

        conditions.extend([
            platform, gpu, resolution, version, reproducibility,
            applicability_check, status_check,
        ])
        for item in (platform, gpu, resolution, version, reproducibility):
            if item.status is CheckStatus.unknown:
                limitations.append(f"{item.check_id.split('.', 1)[1]} is unknown:"
                                   f" {item.note or 'no evidence'}")
            elif item.status is CheckStatus.fail:
                limitations.append(f"{item.check_id.split('.', 1)[1]} differs from"
                                   f" this context: {item.note or 'no evidence'}")
        if applicability_check.status is not CheckStatus.pass_:
            limitations.append(
                "the source does not claim applicability; the candidate is not direct"
            )
        if status == "needs_followup":
            limitations.append("the source marks the configuration as needing follow-up")
        elif status is None:
            limitations.append("the source does not classify the configuration")
        if not performance_basis:
            limitations.append(
                "no measurement with a statistic definition supports this candidate"
            )
        if not visual_basis:
            limitations.append(
                "no text visual evidence is available; images were not read"
            )
        duplicate = [item for item in independence if item.status is CheckStatus.fail]
        if duplicate:
            limitations.append(
                "duplicate source: reposted records are counted once"
            )
        if unresolved:
            limitations.append("the configuration references unresolved package members")
        relation = self._relation_check(environment)
        conditions.append(relation)
        if relation.status is not CheckStatus.pass_:
            limitations.append(
                "no sourced hardware or technology relation supports a neighbour match"
            )

        match_class = self._match_class(
            applicability, status, platform, gpu, resolution, version,
            reproducibility, relation,
        )
        reason = (
            f"{record.local_record_id}: match={match_class}; "
            + ", ".join(
                f"{item.check_id.split('.', 1)[1]}={item.status.value}"
                for item in (platform, gpu, resolution, version, reproducibility)
            )
            + f"; {len(assignments)} assignment(s);"
            f" {len(performance_basis)} measurement evidence(s)"
        )
        return CandidateAssessment(
            configuration_ref=_record_ref(gate.package.id, record),
            match_class=match_class,
            condition_checks=conditions,
            visual_basis=visual_basis,
            performance_basis=performance_basis,
            independence_checks=independence,
            selection_reason=reason,
            limitations=limitations,
        )

    @staticmethod
    def _compare(check_id: str, actual: str | None, stated: str | None) -> CheckResult:
        if actual is None or stated is None:
            return _check(
                check_id, CheckStatus.unknown,
                note=f"context={actual!r}, source={stated!r}",
            )
        if actual == stated:
            return _check(check_id, CheckStatus.pass_, note=f"{actual} matches")
        return _check(
            check_id, CheckStatus.fail,
            note=f"context={actual!r}, source={stated!r}",
        )

    @staticmethod
    def _applicability(applicability: Any) -> CheckResult:
        if isinstance(applicability, str) and applicability in _POSITIVE_APPLICABILITY:
            return _check("candidate.applicability", CheckStatus.pass_,
                          note=applicability)
        if applicability == "fail":
            return _check("candidate.applicability", CheckStatus.fail,
                          note="the source records a failed applicability")
        return _check(
            "candidate.applicability", CheckStatus.unknown,
            note=f"applicability={applicability!r}",
        )

    @staticmethod
    def _reproducibility(
        payload: Mapping[str, Any],
        assignments: Sequence[AssignmentV01],
        by_local: Mapping[tuple[str, str], ImportedRecord],
        scope: str,
    ) -> tuple[CheckResult, bool]:
        unresolved = False
        for assignment in assignments:
            if assignment.value.value_kind != "fixed":
                return (
                    _check(
                        "candidate.reproducibility", CheckStatus.unknown,
                        note="the configuration is not fully resolved",
                    ),
                    False,
                )
            setting = by_local.get((scope, assignment.setting_id))
            if setting is None or setting.record_type != "settings":
                unresolved = True
            for evidence_id in assignment.evidence_ids:
                member = by_local.get((scope, evidence_id))
                if member is None or member.record_type != "evidence":
                    unresolved = True
        if unresolved:
            return (
                _check(
                    "candidate.reproducibility", CheckStatus.fail,
                    note="an assignment references a record outside the package",
                ),
                True,
            )
        if payload.get("source_capture_complete") is True and (
            payload.get("runtime_complete") is True
        ):
            return (
                _check("candidate.reproducibility", CheckStatus.pass_,
                       note="the whole configuration is reproducible"),
                False,
            )
        return (
            _check(
                "candidate.reproducibility", CheckStatus.unknown,
                note="the source does not prove the whole configuration is"
                     " reproducible",
            ),
            False,
        )

    @staticmethod
    def _basis(
        package: KnowledgePackage,
        assignments: Sequence[AssignmentV01],
        by_local: Mapping[tuple[str, str], ImportedRecord],
        scope: str,
        kind: str,
    ) -> tuple[list[EvidenceRef], list[ImportedRecord]]:
        refs: list[EvidenceRef] = []
        records: list[ImportedRecord] = []
        seen: set[str] = set()
        for assignment in assignments:
            for evidence_id in assignment.evidence_ids:
                record = by_local.get((scope, evidence_id))
                if record is None or record.record_type != "evidence":
                    continue
                payload = record.payload if isinstance(record.payload, Mapping) else {}
                if kind == "performance":
                    wanted = (
                        payload.get("kind") == "measurement"
                        and isinstance(payload.get("metric"), str)
                        and isinstance(payload.get("numeric"), Mapping)
                    )
                else:
                    wanted = (
                        payload.get("kind") in _VISUAL_EVIDENCE_KINDS
                        and isinstance(payload.get("statement"), str)
                        and bool(payload.get("statement").strip())
                    )
                if not wanted or record.id in seen:
                    continue
                seen.add(record.id)
                refs.append(_knowledge_evidence(package.id, record))
                records.append(record)
        return refs, records

    def _independence(
        self,
        package: KnowledgePackage,
        records: Sequence[ImportedRecord],
        source_facts: Mapping[str, tuple[str | None, str | None, bool]],
    ) -> list[CheckResult]:
        if len(records) < 2:
            return [_check(
                "candidate.independence", CheckStatus.not_applicable,
                note="a single source cannot be duplicated",
            )]
        urls: dict[str, int] = {}
        hashes: dict[str, int] = {}
        for record in records:
            url, content_hash, _proved = source_facts.get(
                record.source_revision_id, (None, None, False)
            )
            if url:
                urls[url] = urls.get(url, 0) + 1
            if content_hash:
                hashes[content_hash] = hashes.get(content_hash, 0) + 1
        duplicate_url = any(count > 1 for count in urls.values())
        duplicate_hash = any(count > 1 for count in hashes.values())
        if duplicate_url or duplicate_hash:
            return [_check(
                "candidate.independence", CheckStatus.fail,
                note="reposted sources share a canonical URL or content hash",
            )]
        return [_check(
            "candidate.independence", CheckStatus.pass_,
            note="the supporting records come from distinct sources",
        )]

    @staticmethod
    def _relation_check(environment: ImportedRecord | None) -> CheckResult:
        if environment is None or not isinstance(environment.payload, Mapping):
            return _check(
                "candidate.neighbor_relation", CheckStatus.unknown,
                note="no environment record states a relation",
            )
        fields = environment.payload.get("fields")
        if not isinstance(fields, Mapping):
            return _check(
                "candidate.neighbor_relation", CheckStatus.unknown,
                note="no environment record states a relation",
            )
        relation = fields.get("hardware_relation")
        if not isinstance(relation, Mapping):
            return _check(
                "candidate.neighbor_relation", CheckStatus.unknown,
                note="no sourced hardware relation is stated",
            )
        proof = relation.get("proof")
        if not relation.get("value") or not isinstance(proof, Mapping) or not proof:
            return _check(
                "candidate.neighbor_relation", CheckStatus.unknown,
                note="the relation has no source scope",
            )
        return _check(
            "candidate.neighbor_relation", CheckStatus.pass_,
            note=str(relation.get("value")),
        )

    @staticmethod
    def _match_class(
        applicability: Any,
        status: Any,
        platform: CheckResult,
        gpu: CheckResult,
        resolution: CheckResult,
        version: CheckResult,
        reproducibility: CheckResult,
        relation: CheckResult,
    ) -> str:
        critical = (platform, gpu, resolution, version, reproducibility)
        if status in (None, "needs_followup"):
            return "conditional"
        if gpu.status is CheckStatus.fail or resolution.status is CheckStatus.fail \
                or version.status is CheckStatus.fail:
            return "neighbor" if relation.status is CheckStatus.pass_ else "general"
        if all(item.status is CheckStatus.pass_ for item in critical) and (
            applicability in _POSITIVE_APPLICABILITY
        ):
            return "direct"
        return "conditional"

    @staticmethod
    def _source_facts(
        uow: UnitOfWork, records: Sequence[ImportedRecord]
    ) -> dict[str, tuple[str | None, str | None, bool]]:
        """Source revision id -> (canonical url, content hash, proved)."""
        from game_app.contracts.knowledge import Source, SourceRevision

        facts: dict[str, tuple[str | None, str | None, bool]] = {}
        for record in records:
            revision_id = record.source_revision_id
            if revision_id in facts:
                continue
            stored = uow.find_object("SourceRevision", revision_id)
            if stored is None:
                facts[revision_id] = (None, None, False)
                continue
            revision = SourceRevision.model_validate_json(stored.payload_bytes)
            source_stored = uow.find_object("Source", revision.source_id)
            url = None
            if source_stored is not None:
                url = Source.model_validate_json(
                    source_stored.payload_bytes
                ).canonical_url
            facts[revision_id] = (url, revision.content_hash, True)
        return facts

    def _screen_model_candidates(
        self,
        model_candidates: Sequence[Any],
        records: Sequence[ImportedRecord],
        diagnostics: list[str],
    ) -> None:
        members = {record.id: record for record in records}
        for item in model_candidates:
            identifier = (
                _field(item, "record_id")
                or _field(item, "record_snapshot_id")
                or _field(item, "local_record_id")
            )
            if isinstance(identifier, str) and identifier in members:
                diagnostics.append(
                    f"the model named pool member {identifier}; the deterministic"
                    " assessment stands"
                )
            else:
                diagnostics.append(
                    f"model candidate {identifier!r} refers to a record outside the"
                    " package; it is kept as a diagnostic, not a candidate"
                )


# --- module-level read-only entries ----------------------------------------


def assess_candidates(
    task_input: RecommendationInput,
    *,
    repository: Any,
    knowledge: Any,
    user_scope_id: str,
    adoption: Any | None = None,
    performance: Any | None = None,
    clock=utc_now,
    model_candidates: Sequence[Any] | None = None,
) -> list[CandidateAssessment]:
    """The read-only candidate assessment (03 §5); no write, no provider call."""
    gate = InputGate(
        repository, knowledge, adoption, performance, clock=clock,
    )
    result = gate.check(task_input, user_scope_id=user_scope_id)
    builder = CandidateBuilder(repository, knowledge, clock=clock)
    return builder.build(
        result, model_candidates=model_candidates,
    ).assessments


def assessment_manifest(assessments: Sequence[CandidateAssessment]) -> bytes:
    """A byte-stable manifest of the candidate decision (03 §4).

    ``checked_at`` records when a check ran, not what it concluded, so it is
    normalised out: the same input must produce the same bytes.
    """
    payload = []
    for assessment in sorted(
        assessments, key=lambda item: item.configuration_ref.record_snapshot_id
    ):
        item = assessment.model_dump(mode="json")
        for key in ("condition_checks", "independence_checks"):
            for check in item[key]:
                check["checked_at"] = None
        payload.append(item)
    return canonical_json(payload)


def resolve_setting_identities(
    records: Sequence[ImportedRecord],
    mappings: Sequence[SettingIdentityMapping],
) -> dict[str, str]:
    """Setting record id -> canonical identity key.

    Only a mapping whose every check passed merges settings; an unmapped setting
    keeps its own record id, so two same-named settings never share a group.
    """
    settings = {
        record.id: record for record in records if record.record_type == "settings"
    }
    resolved = {record_id: record_id for record_id in settings}
    for mapping in mappings:
        if not mapping.checks or any(
            item.status is not CheckStatus.pass_ for item in mapping.checks
        ):
            continue
        for ref in mapping.source_setting_refs:
            if ref.record_snapshot_id in settings:
                resolved[ref.record_snapshot_id] = mapping.id
    return resolved


def validate_actions(
    task_input: RecommendationInput,
    actions: Sequence[Any],
    *,
    repository: Any,
    knowledge: Any,
    user_scope_id: str,
    package: KnowledgePackage | None = None,
    clock=utc_now,
) -> list[CheckResult]:
    """Shape, evidence and dependency-group validation (03 §5, base form)."""
    if package is None:
        package = knowledge.get_package(
            task_input.package_id, user_scope_id=user_scope_id,
            verify_artifacts=False,
        )
    if package.publication_status is not PublicationStatus.published:
        raise ContractError(
            ErrorCode.REFERENCE_INVALID,
            "an unpublished package is not a recommendation input",
        )
    members = set(package.record_snapshot_ids)
    checks: list[CheckResult] = []
    groups: dict[str, int] = {}
    for action in actions:
        action_id = _field(action, "action_id")
        kind = _field(action, "action")
        setting_ref = _field(action, "setting_ref")
        value = _field(action, "value")
        evidence_refs = _field(action, "evidence_refs") or []
        menu_path = _field(action, "menu_path") or []
        group = _field(action, "dependency_group_id")

        checks.append(_action_ref_check(action_id, setting_ref, members, clock))
        if kind in ("set_value", "conditional_strategy"):
            # A value's shape and its evidence only exist for an executable action;
            # an instruction to check or defer has no value to shape.
            checks.append(_action_value_check(action_id, kind, value, clock))
            checks.append(
                _action_evidence_check(action_id, kind, evidence_refs, clock)
            )
        if kind in ("set_value", "conditional_strategy") and not menu_path:
            checks.append(_check(
                "operation.menu", CheckStatus.unknown, clock=clock,
                note=f"{action_id} states no menu path",
            ))
        else:
            checks.append(_check(
                "operation.menu", CheckStatus.pass_, clock=clock,
                note=f"{action_id} menu path known",
            ))
        checks.append(_check(
            "operation.check_in_game",
            CheckStatus.pass_ if kind == "check_in_game" else CheckStatus.not_applicable,
            clock=clock,
            note=f"{action_id}",
        ))
        restart = _field(action, "restart_required")
        checks.append(_check(
            "restart.required", CheckStatus.unknown, clock=clock,
            note=f"{action_id} restart requirement is unknown"
            if restart is None else f"{action_id} restart requirement recorded",
        ))
        if isinstance(group, str) and group:
            groups[group] = groups.get(group, 0) + 1

    for group, count in groups.items():
        checks.append(_check(
            "combination.dependency_group",
            CheckStatus.pass_ if count > 1 else CheckStatus.fail,
            clock=clock,
            note=f"{group} has {count} member(s)",
        ))
    return checks


def _action_value_check(
    action_id: Any, kind: Any, value: Any, clock
) -> CheckResult:
    if kind == "set_value":
        raw = _value_mapping(value)
        fixed = raw.get("value_kind") == "fixed" and raw.get("value") is not None
        return _check(
            "value.shape", CheckStatus.pass_ if fixed else CheckStatus.fail,
            clock=clock,
            note=f"{action_id} set_value is fixed" if fixed
            else f"{action_id} set_value needs a fixed, non-null value",
        )
    if kind == "conditional_strategy":
        raw = _value_mapping(value)
        ok = raw.get("value_kind") in ("conditional", "alternatives")
        return _check(
            "value.shape", CheckStatus.pass_ if ok else CheckStatus.fail,
            clock=clock,
            note=f"{action_id} conditional strategy is explicit" if ok
            else f"{action_id} conditional_strategy needs a conditional value",
        )
    return _check(
        "value.shape", CheckStatus.not_applicable, clock=clock,
        note=f"{action_id} is not an executable value",
    )


def _action_evidence_check(
    action_id: Any, kind: Any, evidence_refs: Any, clock
) -> CheckResult:
    if kind not in ("set_value", "conditional_strategy"):
        return _check(
            "evidence.present", CheckStatus.not_applicable, clock=clock,
            note=f"{action_id} states what to check instead of a value",
        )
    present = bool(evidence_refs)
    return _check(
        "evidence.present", CheckStatus.pass_ if present else CheckStatus.fail,
        clock=clock,
        note=f"{action_id} cites supporting evidence" if present
        else f"{action_id} cites no evidence; a reason is not evidence",
    )


def _action_ref_check(
    action_id: Any, setting_ref: Any, members: set[str], clock
) -> CheckResult:
    kind = _field(setting_ref, "kind")
    if kind == "knowledge":
        record = _field(setting_ref, "record")
        record_id = _field(record, "record_snapshot_id")
        resolves = record_id in members
        return _check(
            "reference.setting", CheckStatus.pass_ if resolves else CheckStatus.fail,
            clock=clock,
            note=f"{action_id} setting resolves to a package member" if resolves
            else f"{action_id} setting is not a package member",
        )
    if kind == "local":
        return _check(
            "reference.setting", CheckStatus.unknown, clock=clock,
            note=f"{action_id} names a local setting; the catalog is checked at publish",
        )
    return _check(
        "reference.setting", CheckStatus.fail, clock=clock,
        note=f"{action_id} has no valid setting reference",
    )


def _value_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, SettingValueV01):
        return value.model_dump(mode="json")
    return {}


__all__ = [
    "GateResult",
    "InputGate",
    "ConstraintEvaluation",
    "ConstraintExclusion",
    "ConstraintEvaluator",
    "CandidateBuildResult",
    "CandidateBuilder",
    "assess_candidates",
    "assessment_manifest",
    "resolve_setting_identities",
    "validate_actions",
]
