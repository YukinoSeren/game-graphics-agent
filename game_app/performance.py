"""Performance sessions, segments and immutable configuration bindings.

Owned by 04.  This module records what the player declared and what the backend
clock measured: it never reads a game file, never probes hardware and never
calls a model.  An unknown configuration stays ``unverified``; closing a
segment appends a new binding revision instead of rewriting the open one.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID, uuid5

from game_app.adapters.capture import CaptureAdapterRegistry, CaptureHandle
from game_app.adapters.presentmon import LogParseResult, default_registry, parse_log

from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    FieldValue,
    MutationContext,
    Provenance,
    ResourceResult,
)
from game_app.contracts.configuration import ContextDeclarationInput, PlayerDeclaration
from game_app.contracts.device import Resolution
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    ComparabilityLevel,
    ConfigurationState,
    FrameStreamKind,
    SessionLifecycle,
    TERMINAL_TASK_STATES,
    ThresholdMode,
    ValueState,
)
from game_app.contracts.performance import (
    BindConfigurationRequest,
    CaptureArtifact,
    ConfigurationBinding,
    CreateSessionRequest,
    CreateTestProtocolRequest,
    EndSessionRequest,
    EndSegmentRequest,
    HistoricalLogTarget,
    LogAssociationRequest,
    LogInspection,
    LogStreamCandidate,
    MetricCatalogQuery,
    MetricChoicePage,
    MetricDefinition,
    PlaySession,
    RunConditions,
    RunDeclarationInput,
    SessionSegment,
    SplitSegmentRequest,
    StreamSelection,
    TestProtocol,
    TimeMapping,
)
from game_app.contracts.primitives import (
    BindingRange,
    BindingRef,
    ObjectRef,
    Scalar,
    TimeRange,
    utc_now,
)
from game_app.errors import ContractError, ErrorCode, FieldError, empty_details
from game_app.files import FileService, PublishHooks
from game_app import metrics
from game_app.analyzer import (
    ANALYZER_VERSION,
    CLEANING_PROTOCOL_VERSION,
    TAIL_SAMPLE_FLOOR,
    ContinuousRun,
    adaptive_threshold_ms,
    clean_frames,
    cleaning_manifest,
    long_frame_statistics,
    mean_present_rate,
    percentile_nearest_rank,
    quality_for_sample,
    slowest_tail_inverse_mean,
    tail_sample_count,
)
from game_app.contracts.performance import (
    AdaptiveLongFrame,
    CaptureLease,
    CompareInput,
    ComparisonProtocol,
    CreateComparisonProtocolRequest,
    MetricComparison,
    MetricDenominator,
    MetricValue,
    NumericValueV01,
    PerformanceComparison,
    PerformanceSummary,
    StopCaptureInput,
)
from game_app.contracts.tasks import (
    AnalysisInput,
    CaptureInput,
    DomainResult,
    TaskStopReason,
)
from game_app.contracts.common import TaskAccepted
from game_app.contracts.enums import LogQuality, TaskState, TaskType
from game_app.tasks import HandlerOutcome, TaskContext
from game_app.repository import ProductRepository, UnitOfWork, canonical_json

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
SESSION_RULE_VERSION = "performance-session/0.4"
SESSION_ROUTE = "/api/play-sessions"
SESSION_END_ROUTE = "/api/play-sessions/{session_id}/end"
SEGMENT_END_ROUTE = "/api/session-segments/{segment_id}/end"
SEGMENT_SPLIT_ROUTE = "/api/session-segments/{segment_id}/split"
SEGMENT_BIND_ROUTE = "/api/session-segments/{segment_id}/bindings"
TEST_PROTOCOL_ROUTE = "/api/test-protocols"
TEST_PROTOCOL_RULE_VERSION = "test-protocol/0.4"
LOG_ASSOCIATION_ROUTE = "/api/log-associations"
LOG_ASSOCIATION_RULE_VERSION = "log-association/0.4"
ANALYSIS_ROUTE = "/api/analyses"
ANALYSIS_RULE_VERSION = "analysis/0.4"
COMPARISON_ROUTE = "/api/comparisons"
CAPTURE_ROUTE = "/api/captures"
CAPTURE_STOP_ROUTE = "/api/captures/{task_id}/stop"
CAPTURE_RULE_VERSION = "capture/0.4"
COMPARISON_PROTOCOL_ROUTE = "/api/comparison-protocols"
COMPARISON_PROTOCOL_RULE_VERSION = "comparison-protocol/0.4"
COMPARISON_RULE_VERSION = "comparison/0.4"
LONG_FRAME_CLUSTERING_VERSION = "perf-long-frame-clustering/1"
LONG_FRAME_DENOMINATOR_VERSION = "perf-long-frame-denominator/1"
DEFAULT_COMPARISON_THRESHOLD_MS = 50.0
RECOMPUTE_DEADLINE_SECONDS = 120.0
LOG_TARGET_TIME_BASIS = "session"
SESSION_TIME_BASIS = "session"

_ID_NAMESPACE = UUID("9c3d7b0e-6a24-4f18-9e53-1b7c2d5a4e60")
_METRIC_ID_NAMESPACE = UUID("4b1e2f7a-3c58-4d90-8a61-7e2d9c0b5f13")

_PAIR_WIDTHS = {"output_width": "output_height", "output_height": "output_width",
                "render_width": "render_height", "render_height": "render_width"}
_FIXED_CONDITIONS = frozenset({
    "output_width", "output_height", "render_width", "render_height",
    "frame_generation", "upscaling", "scenario",
})
# ``scenario`` is a separate test-protocol field and must not be repeated in
# the condition map (04 §4.4).
_PROTOCOL_CONDITIONS = _FIXED_CONDITIONS - {"scenario"}


def _deterministic_id(*parts: str) -> str:
    return uuid5(_ID_NAMESPACE, ":".join(parts)).hex


def _request_hash(route: str, schema_version: str, request: Any) -> str:
    return hashlib.sha256(canonical_json({
        "schema_version": schema_version,
        "method": "POST",
        "route": route,
        "request": request.model_dump(mode="json"),
    })).hexdigest()


def _check(check_id: str, status: CheckStatus, note: str, now: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=SESSION_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else now,
        method="deterministic session rule",
        note=note,
    )


def _declaration_evidence(managed: Any) -> EvidenceRef:
    return EvidenceRef(
        kind="artifact",
        file_id=managed.id,
        content_hash=managed.manifest.sha256,
        locator=managed.manifest.relative_name,
    )


def _unknown(value_type: Any) -> FieldValue[Any]:
    """A condition nobody declared: unknown, never a default value."""
    return FieldValue[value_type](
        value_state=ValueState.unknown,
        value=None,
        unknown_reason="not_provided",
        acquisition_status=AcquisitionStatus.not_attempted,
    )


def _known(value_type: Any, value: Any, evidence: EvidenceRef, now: str) -> FieldValue[Any]:
    return FieldValue[value_type](
        value_state=ValueState.known,
        value=value,
        acquisition_status=AcquisitionStatus.succeeded,
        provenance=[Provenance(
            method=AcquisitionMethod.user_provided,
            observed_at=None,
            recorded_at=now,
            evidence_refs=[evidence],
            note="player declaration",
        )],
    )


def _invalid(path: str, code: str, message: str) -> ContractError:
    return ContractError(
        ErrorCode.INPUT_VALIDATION_FAILED,
        message,
        field_errors=[FieldError(path=path, code=code, message=message)],
    )


class PerformanceService:
    """Synchronous 04 resources: sessions, segments and configuration bindings."""

    def __init__(
        self,
        repository: ProductRepository,
        files: FileService,
        *,
        clock: Callable[[], str] = utc_now,
        log_registry: Any = None,
        tasks: Any = None,
        capture_registry: CaptureAdapterRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.files = files
        self.clock = clock
        # Only formats verified against an offline sample are enabled; the
        # default registry does not claim PresentMon support.
        self.log_registry = log_registry or default_registry()
        # Set by Application: analysis is registered as a durable task, so the
        # submission guards can run *before* a key is consumed.
        self.tasks = tasks
        # None means capture is not wired at all (its handler stays
        # unregistered); an empty registry still refuses every adapter.
        self.capture_registry = capture_registry

    # -- reads (every read resolves the caller's ownership) ------------------
    def get_session(self, session_id: str, user_scope_id: str) -> PlaySession:
        with self.repository.unit_of_work() as uow:
            session = self._load_session(uow, session_id)
            self._require_session_owner(uow, session, user_scope_id)
            return session

    def get_segment(self, segment_id: str, user_scope_id: str) -> SessionSegment:
        with self.repository.unit_of_work() as uow:
            segment = self._load_segment(uow, segment_id)
            session = self._load_session(uow, segment.session_id)
            self._require_session_owner(uow, session, user_scope_id)
            return segment

    def get_binding(
        self, segment_id: str, binding_revision: int, user_scope_id: str
    ) -> ConfigurationBinding:
        with self.repository.unit_of_work() as uow:
            segment = self._load_segment(uow, segment_id)
            session = self._load_session(uow, segment.session_id)
            self._require_session_owner(uow, session, user_scope_id)
            return self._load_binding(uow, segment.id, binding_revision)

    def require_analyzeable(
        self, binding_ref: BindingRef, user_scope_id: str
    ) -> None:
        """Only the *named* closed segment and closed binding may be analyzed.

        The requested revisions are not compared with the head: a later rebind
        must not invalidate an artifact that was captured in an earlier, closed
        revision (04 §4.3 fixes the BindingRef at capture time).
        """
        with self.repository.unit_of_work() as uow:
            segment = self._load_segment(uow, binding_ref.segment_id)
            session = self._load_session(uow, segment.session_id)
            self._require_session_owner(uow, session, user_scope_id)
            named = self._load_segment_revision(
                uow, segment.id, binding_ref.segment_revision
            )
            if named.lifecycle is not SessionLifecycle.ended or named.end_s is None:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "an open segment revision cannot be analyzed; end it first",
                )
            binding = self._load_binding(uow, segment.id, binding_ref.binding_revision)
            if binding.effective_range.end is None:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "an open binding cannot be analyzed; close it first",
                )

    # -- session creation ---------------------------------------------------
    def create_session(
        self,
        request: CreateSessionRequest,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        route = SESSION_ROUTE
        request_hash = _request_hash(route, request_schema_version, request)
        # A registered key replays its original response; new-submission guards
        # must not run again for it.
        if not self._has_registered(context, route):
            self._validate_creation_inputs(request, context.user_scope_id)
        ids = {
            role: _deterministic_id(
                context.user_scope_id, route, context.idempotency_key, request_hash, role,
            )
            for role in ("session", "segment", "binding", "declaration")
        }
        manifest = {
            "kind": "session_creation",
            "rule_version": SESSION_RULE_VERSION,
            "request": request.model_dump(mode="json"),
            "session_id": ids["session"],
            "segment_id": ids["segment"],
            "binding_id": ids["binding"],
            "declaration_id": ids["declaration"],
        }
        published = self.files.publish(
            kind="import",
            files={f"sessions/{ids['session']}/declaration.json": canonical_json(manifest)},
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=route,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_creation(
                uow, request=request, user_scope_id=context.user_scope_id,
                ids=ids, managed=managed,
            ),
            hooks=hooks,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _validate_creation_inputs(
        self, request: CreateSessionRequest, user_scope_id: str
    ) -> None:
        with self.repository.unit_of_work() as uow:
            installation = self._installation(uow, request.installation_id, user_scope_id)
            self._context(uow, request.game_context_id, installation)
            self._device_snapshot(uow, request.device_snapshot_id, user_scope_id)
            self._applied(uow, request.applied_id, installation, user_scope_id)
            self._test_protocol_ref(
                uow, request.test_protocol_id, installation["game_id"]
            )
            self._recommendation(uow, request.recommendation_id)
        self._require_declaration_installation(request.run_declaration, request.installation_id)
        self._refuse_unverifiable_configuration(
            request.applied_id, request.configuration_state, request.run_declaration.fields
        )
        # Field rules run before anything is staged: an invalid submission must
        # not leave a frozen file that a corrected same-key retry then collides
        # with (error-conventions §4.2).
        _validate_condition_fields(
            request.run_declaration.fields, "run_declaration.fields", allow_scenario=True
        )

    def _write_creation(
        self,
        uow: UnitOfWork,
        *,
        request: CreateSessionRequest,
        user_scope_id: str,
        ids: Mapping[str, str],
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        installation = self._installation(uow, request.installation_id, user_scope_id)
        context = self._context(uow, request.game_context_id, installation)
        self._device_snapshot(uow, request.device_snapshot_id, user_scope_id)
        self._applied(uow, request.applied_id, installation, user_scope_id)
        self._test_protocol_ref(uow, request.test_protocol_id, installation["game_id"])
        self._recommendation(uow, request.recommendation_id)
        self._require_declaration_installation(
            request.run_declaration, installation["series_id"]
        )
        self._refuse_unverifiable_configuration(
            request.applied_id, request.configuration_state, request.run_declaration.fields
        )

        now = self.clock()
        evidence = _declaration_evidence(managed[0])
        conditions, scenario = _build_run_conditions(request.run_declaration.fields, evidence, now)
        declared = ContextDeclarationInput(
            kind="context_declaration",
            installation_id=installation["series_id"],
            game_id=installation["game_id"],
            fields=request.run_declaration.fields,
        )
        declaration = PlayerDeclaration(
            id=ids["declaration"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            installation_id=installation["series_id"],
            input=declared,
            raw_text=request.run_declaration.raw_text,
            submitted_at=now,
            declaration_file_id=managed[0].id,
        )
        session = PlaySession(
            id=ids["session"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            installation_id=installation["series_id"],
            game_id=installation["game_id"],
            device_snapshot_id=request.device_snapshot_id,
            game_context_id=request.game_context_id,
            started_at=now,
            ended_at=None,
            revision=1,
            lifecycle=SessionLifecycle.active,
            initial_applied_id=request.applied_id,
            recommendation_id=request.recommendation_id,
            configuration_state=request.configuration_state,
            scenario=scenario,
            target_proposal_ref=None,
            test_protocol_id=request.test_protocol_id,
            run_conditions=conditions,
        )
        segment = SessionSegment(
            id=ids["segment"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            session_id=session.id,
            start_s=0.0,
            end_s=None,
            time_basis=SESSION_TIME_BASIS,
            revision=1,
            lifecycle=SessionLifecycle.active,
            applied_id=request.applied_id,
            configuration_state=request.configuration_state,
            binding_revision=1,
            scenario=scenario,
            excluded_ranges=[],
            stream_selection=_unconfirmed_stream(now),
            run_conditions=conditions,
        )
        binding = ConfigurationBinding(
            id=ids["binding"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            segment_id=segment.id,
            binding_revision=1,
            applied_id=request.applied_id,
            configuration_state=request.configuration_state,
            effective_range=BindingRange(
                start=0.0, end=None, time_basis=SESSION_TIME_BASIS, unit="s",
            ),
            evidence_refs=[evidence] if request.applied_id is not None else [],
            supersedes_revision=None,
        )

        uow.insert_object("PlayerDeclaration", declaration)
        uow.insert_object("PlaySession", session)
        uow.insert_object("SessionSegment", segment)
        uow.insert_object("ConfigurationBinding", binding)
        uow.append_snapshot("PlaySession", session.id, session)
        uow.append_snapshot("SessionSegment", segment.id, segment)
        self._insert_segment_head(uow, segment, segment.id, now)
        self._insert_binding_row(uow, binding, now)
        self._write_creation_references(
            uow, session=session, segment=segment, declaration=declaration,
            installation=installation, context=context,
        )
        uow.record_domain_event(
            target_type="PlaySession", target_id=session.id, dimension="session",
            sequence=1, previous_state=None, next_state=SessionLifecycle.active.value,
            cause="session_created", related_refs=[
                ObjectRef(entity_type="PlayerDeclaration", entity_id=declaration.id),
            ], created_at=now,
        )
        uow.record_domain_event(
            target_type="SessionSegment", target_id=segment.id, dimension="session",
            sequence=1, previous_state=None, next_state=SessionLifecycle.active.value,
            cause="segment_started",
            related_refs=[ObjectRef(entity_type="PlaySession", entity_id=session.id)],
            created_at=now,
        )
        return [
            ObjectRef(entity_type="PlayerDeclaration", entity_id=declaration.id),
            ObjectRef(entity_type="PlaySession", entity_id=session.id),
            ObjectRef(entity_type="SessionSegment", entity_id=segment.id),
            ObjectRef(entity_type="ConfigurationBinding", entity_id=binding.id),
            ObjectRef(entity_type="ManagedFile", entity_id=managed[0].id),
        ]

    # -- ending one segment -------------------------------------------------
    def end_segment(
        self,
        request: EndSegmentRequest,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
    ) -> ResourceResult:
        route = SEGMENT_END_ROUTE.format(segment_id=request.segment_id)
        request_hash = _request_hash(route, request_schema_version, request)
        with self.repository.unit_of_work() as uow:
            replay = self._replay_resource(uow, context, route, request_schema_version, request_hash)
            if replay is not None:
                return replay
            segment, session = self._segment_and_session(uow, request.segment_id, context.user_scope_id)
            self._require_active_segment(segment, request)
            end_s = self._elapsed_seconds(session, segment)
            binding = self._close_segment(uow, segment, session, end_s, now=self.clock())
            updated = segment.model_copy(update={
                "revision": segment.revision + 1,
                "end_s": end_s,
                "lifecycle": SessionLifecycle.ended,
                "binding_revision": binding.binding_revision,
            })
            self._write_segment_revision(uow, segment, updated, session, cause="segment_ended")
            result = ResourceResult(refs=[
                ObjectRef(entity_type="SessionSegment", entity_id=updated.id),
                ObjectRef(entity_type="ConfigurationBinding", entity_id=binding.id),
            ])
            self._record_resource(uow, context, route, request_schema_version, request_hash, result)
            return result

    # -- splitting a segment ------------------------------------------------
    def split_segment(
        self,
        request: SplitSegmentRequest,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        route = SEGMENT_SPLIT_ROUTE.format(segment_id=request.ending.segment_id)
        request_hash = _request_hash(route, request_schema_version, request)
        if not self._has_registered(context, route):
            self._validate_split(request, context.user_scope_id)
        ids = {
            role: _deterministic_id(
                context.user_scope_id, route, context.idempotency_key, request_hash, role,
            )
            for role in ("segment", "binding", "declaration")
        }
        manifest = {
            "kind": "segment_split",
            "rule_version": SESSION_RULE_VERSION,
            "request": request.model_dump(mode="json"),
            "segment_id": ids["segment"],
            "binding_id": ids["binding"],
            "declaration_id": ids["declaration"],
        }
        published = self.files.publish(
            kind="import",
            files={f"segments/{ids['segment']}/declaration.json": canonical_json(manifest)},
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=route,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_split(
                uow, request=request, user_scope_id=context.user_scope_id,
                ids=ids, managed=managed,
            ),
            hooks=hooks,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _validate_split(
        self, request: SplitSegmentRequest, user_scope_id: str
    ) -> None:
        """Every guard runs before staging, so a conflict leaves the key free."""
        with self.repository.unit_of_work() as uow:
            old, session = self._segment_and_session(
                uow, request.ending.segment_id, user_scope_id
            )
            self._require_active_segment(old, request.ending)
            self._require_declaration_installation(
                request.run_declaration, session.installation_id
            )
            if request.new_applied_id is not None and self._applied_by_installation(
                uow, request.new_applied_id, session.installation_id
            ) is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the applied configuration does not belong to this installation",
                )
        self._refuse_unverifiable_configuration(
            request.new_applied_id, request.configuration_state,
            request.run_declaration.fields,
        )
        _validate_condition_fields(
            request.run_declaration.fields, "run_declaration.fields", allow_scenario=True
        )

    def _write_split(
        self,
        uow: UnitOfWork,
        *,
        request: SplitSegmentRequest,
        user_scope_id: str,
        ids: Mapping[str, str],
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        old, session = self._segment_and_session(
            uow, request.ending.segment_id, user_scope_id
        )
        self._require_active_segment(old, request.ending)
        installation_id = session.installation_id
        self._require_declaration_installation(request.run_declaration, installation_id)
        self._refuse_unverifiable_configuration(
            request.new_applied_id, request.configuration_state, request.run_declaration.fields
        )
        applied = self._applied_by_installation(uow, request.new_applied_id, installation_id)
        if request.new_applied_id is not None and applied is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the applied configuration does not belong to this installation",
            )

        now = self.clock()
        end_s = self._elapsed_seconds(session, old)
        evidence = _declaration_evidence(managed[0])
        conditions, scenario = _build_run_conditions(request.run_declaration.fields, evidence, now)

        closed = self._close_segment(uow, old, session, end_s, now=now)
        updated_old = old.model_copy(update={
            "revision": old.revision + 1,
            "end_s": end_s,
            "lifecycle": SessionLifecycle.ended,
            "binding_revision": closed.binding_revision,
        })
        self._write_segment_revision(uow, old, updated_old, session, cause="segment_split")

        new_segment = SessionSegment(
            id=ids["segment"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            session_id=session.id,
            start_s=end_s,
            end_s=None,
            time_basis=old.time_basis,
            revision=1,
            lifecycle=SessionLifecycle.active,
            applied_id=request.new_applied_id,
            configuration_state=request.configuration_state,
            binding_revision=1,
            scenario=scenario,
            excluded_ranges=[],
            stream_selection=_unconfirmed_stream(now),
            run_conditions=conditions,
        )
        new_binding = ConfigurationBinding(
            id=ids["binding"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            segment_id=new_segment.id,
            binding_revision=1,
            applied_id=request.new_applied_id,
            configuration_state=request.configuration_state,
            effective_range=BindingRange(
                start=end_s, end=None, time_basis=old.time_basis, unit="s",
            ),
            evidence_refs=[evidence] if applied is not None else [],
            supersedes_revision=None,
        )
        declared = ContextDeclarationInput(
            kind="context_declaration",
            installation_id=installation_id,
            game_id=session.game_id,
            fields=request.run_declaration.fields,
        )
        declaration = PlayerDeclaration(
            id=ids["declaration"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            installation_id=installation_id,
            input=declared,
            raw_text=request.run_declaration.raw_text,
            submitted_at=now,
            declaration_file_id=managed[0].id,
        )
        uow.insert_object("PlayerDeclaration", declaration)
        uow.insert_object("SessionSegment", new_segment)
        uow.insert_object("ConfigurationBinding", new_binding)
        uow.append_snapshot("SessionSegment", new_segment.id, new_segment)
        self._insert_segment_head(uow, new_segment, new_segment.id, now)
        self._insert_binding_row(uow, new_binding, now)
        uow.insert_reference(
            new_segment.id, "session",
            ObjectRef(entity_type="PlaySession", entity_id=session.id),
        )
        uow.record_domain_event(
            target_type="SessionSegment", target_id=new_segment.id, dimension="session",
            sequence=1, previous_state=None, next_state=SessionLifecycle.active.value,
            cause="segment_split",
            related_refs=[ObjectRef(entity_type="PlaySession", entity_id=session.id)],
            created_at=now,
        )
        return [
            ObjectRef(entity_type="PlayerDeclaration", entity_id=declaration.id),
            ObjectRef(entity_type="SessionSegment", entity_id=updated_old.id),
            ObjectRef(entity_type="SessionSegment", entity_id=new_segment.id),
            ObjectRef(entity_type="ConfigurationBinding", entity_id=new_binding.id),
            ObjectRef(entity_type="ManagedFile", entity_id=managed[0].id),
        ]

    # -- ending the session -------------------------------------------------
    def end_session(
        self,
        request: EndSessionRequest,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
    ) -> ResourceResult:
        route = SESSION_END_ROUTE.format(session_id=request.session_id)
        request_hash = _request_hash(route, request_schema_version, request)
        with self.repository.unit_of_work() as uow:
            replay = self._replay_resource(uow, context, route, request_schema_version, request_hash)
            if replay is not None:
                return replay
            session = self._load_session(uow, request.session_id)
            self._require_session_owner(uow, session, context.user_scope_id)
            if session.revision != request.expected_session_revision:
                raise ContractError(
                    ErrorCode.BINDING_REVISION_CONFLICT,
                    "the session changed; re-read it before ending it",
                    field_errors=[FieldError(
                        path="session_revision", code="conflict",
                        message="not the current session revision",
                    )],
                    details=empty_details(
                        expected_revision=request.expected_session_revision,
                        current_revision=session.revision,
                        current_resource_ref=ObjectRef(
                            entity_type="PlaySession", entity_id=session.id,
                        ),
                    ),
                )
            if session.lifecycle is not SessionLifecycle.active:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED, "the session is already ended"
                )
            active = self._active_segments(uow, session.id)
            self._require_exact_segment_set(active, request.active_segments)
            by_id = {segment.id: segment for segment in active}
            ordered = sorted(
                (by_id[item.segment_id] for item in request.active_segments),
                key=lambda segment: segment.start_s,
            )
            end_s = self._elapsed_seconds(session, ordered[0]) if ordered else None
            now = self.clock()
            refs: list[ObjectRef] = [
                ObjectRef(entity_type="PlaySession", entity_id=session.id),
            ]
            segment_refs: list[ObjectRef] = []
            for segment in ordered:
                self._require_current_revisions(
                    segment,
                    expected_segment_revision=next(
                        item.expected_segment_revision for item in request.active_segments
                        if item.segment_id == segment.id
                    ),
                    expected_binding_revision=next(
                        item.expected_binding_revision for item in request.active_segments
                        if item.segment_id == segment.id
                    ),
                )
                binding = self._close_segment(uow, segment, session, end_s, now=now)
                updated = segment.model_copy(update={
                    "revision": segment.revision + 1,
                    "end_s": end_s,
                    "lifecycle": SessionLifecycle.ended,
                    "binding_revision": binding.binding_revision,
                })
                self._write_segment_revision(uow, segment, updated, session, cause="session_ended")
                refs.append(ObjectRef(entity_type="SessionSegment", entity_id=updated.id))
                segment_refs.append(ObjectRef(entity_type="SessionSegment", entity_id=updated.id))
                refs.append(ObjectRef(entity_type="ConfigurationBinding", entity_id=binding.id))

            ended = session.model_copy(update={
                "revision": session.revision + 1,
                "lifecycle": SessionLifecycle.ended,
                "ended_at": now,
            })
            uow.append_snapshot("PlaySession", ended.id, ended)
            uow.record_domain_event(
                target_type="PlaySession", target_id=session.id, dimension="session",
                sequence=ended.revision, previous_state=SessionLifecycle.active.value,
                next_state=SessionLifecycle.ended.value, cause="session_ended",
                related_refs=segment_refs, created_at=now,
            )
            result = ResourceResult(refs=refs)
            self._record_resource(uow, context, route, request_schema_version, request_hash, result)
            return result

    # -- historical binding -------------------------------------------------
    def bind_configuration(
        self,
        request: BindConfigurationRequest,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
    ) -> ResourceResult:
        route = SEGMENT_BIND_ROUTE.format(segment_id=request.segment_id)
        request_hash = _request_hash(route, request_schema_version, request)
        with self.repository.unit_of_work() as uow:
            replay = self._replay_resource(uow, context, route, request_schema_version, request_hash)
            if replay is not None:
                return replay
            segment, session = self._segment_and_session(uow, request.segment_id, context.user_scope_id)
            self._require_current_revisions(
                segment,
                expected_segment_revision=request.expected_segment_revision,
                expected_binding_revision=request.expected_binding_revision,
            )
            if segment.lifecycle is not SessionLifecycle.ended or segment.end_s is None:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "a historical binding needs an ended segment; today's configuration"
                    " cannot be projected into a past period",
                )
            self._evidence_problems(uow, request.evidence_refs, context.user_scope_id)
            if not request.evidence_refs:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "a historical binding needs evidence that applies to the stated period",
                )
            applied = self._applied_by_installation(uow, request.applied_id, session.installation_id)
            if applied is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID, "the bound applied configuration does not exist"
                )
            start = request.effective_range.start
            end = request.effective_range.end
            if (
                request.effective_range.time_basis != segment.time_basis
                or request.effective_range.unit != "s"
                or start < segment.start_s
                or end > segment.end_s
            ):
                raise _invalid(
                    "effective_range", "range",
                    "the bound period must lie inside the ended segment and use its time basis",
                )

            now = self.clock()
            revision = segment.binding_revision + 1
            binding = ConfigurationBinding(
                id=_deterministic_id(
                    context.user_scope_id, route, context.idempotency_key, request_hash,
                    "binding", str(revision),
                ),
                schema_version=PRODUCT_CONTRACT_VERSION,
                created_at=now,
                created_by_task_id=None,
                segment_id=segment.id,
                binding_revision=revision,
                applied_id=request.applied_id,
                configuration_state=ConfigurationState.bound,
                effective_range=BindingRange(
                    start=start, end=end, time_basis=segment.time_basis, unit="s",
                ),
                evidence_refs=list(request.evidence_refs),
                supersedes_revision=segment.binding_revision,
            )
            uow.insert_object("ConfigurationBinding", binding)
            self._insert_binding_row(uow, binding, now)
            updated = segment.model_copy(update={
                "revision": segment.revision + 1,
                "binding_revision": revision,
                "applied_id": request.applied_id,
                "configuration_state": ConfigurationState.bound,
            })
            self._write_segment_revision(
                uow, segment, updated, session, cause="configuration_bound"
            )
            result = ResourceResult(refs=[
                ObjectRef(entity_type="ConfigurationBinding", entity_id=binding.id),
                ObjectRef(entity_type="SessionSegment", entity_id=updated.id),
            ])
            self._record_resource(uow, context, route, request_schema_version, request_hash, result)
            return result

    # -- metric catalog and test protocols (04 §4.4) -----------------------
    def list_metric_definitions(self, query: MetricCatalogQuery) -> MetricChoicePage:
        """Read-only: name every packaged rule, with a registered definition id.

        An empty database returns ``definition_id=null`` and writes nothing;
        the definition is allocated lazily when a protocol is created.
        """
        with self.repository.unit_of_work() as uow:
            ids = {}
            for rule in metrics.RULES:
                found = uow.find_metric_rule(
                    metric_key=rule.metric_key,
                    registry_version=metrics.REGISTRY_VERSION,
                    rule_hash=rule.rule_hash,
                )
                if found is not None:
                    ids[rule.key] = found
        return metrics.catalog(query.frame_stream_kind, ids)

    def get_test_protocol(self, protocol_id: str) -> TestProtocol:
        with self.repository.unit_of_work() as uow:
            return self._load_test_protocol(uow, protocol_id)

    def require_comparison_protocol(self, protocol_id: str) -> None:
        """Type guard for ``CompareInput.protocol_id`` (PERF-T03 consumes it).

        A ``TestProtocol`` id is the wrong entity type and must not be accepted
        by the comparison entry.
        """
        with self.repository.unit_of_work() as uow:
            stored = uow.find_object("ComparisonProtocol", protocol_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced comparison protocol does not exist",
            )

    def create_test_protocol(
        self,
        request: CreateTestProtocolRequest,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        route = TEST_PROTOCOL_ROUTE
        request_hash = _request_hash(route, request_schema_version, request)
        game_id = self._peek_game_id(request.game_context_id)
        if not self._has_registered(context, route):
            self._validate_test_protocol(request, context.user_scope_id)
        protocol_id = _deterministic_id(
            context.user_scope_id, route, context.idempotency_key, request_hash, "protocol",
        )
        now = self.clock()
        definition_ids = {
            rule.key: _metric_definition_id(rule)
            for rule in metrics.RULES
            if rule.key in request.metric_keys
        }
        # The staged declaration must be byte-identical for every retry of the
        # same key: a live registration timestamp here turns a crash-window
        # retry or a double click into IMPORT_SNAPSHOT_INCONSISTENT.  The
        # backend registration moment is the immutable TestProtocol.created_at.
        manifest = {
            "kind": "test_protocol_declaration",
            "rule_version": TEST_PROTOCOL_RULE_VERSION,
            "registry_version": metrics.REGISTRY_VERSION,
            "registry_manifest_hash": metrics.registry_manifest_hash(),
            "game_context_id": request.game_context_id,
            "game_id": game_id,
            "protocol_id": protocol_id,
            "input": request.model_dump(mode="json"),
            "metric_definitions": definition_ids,
        }
        published = self.files.publish(
            kind="import",
            files={f"test-protocols/{protocol_id}/declaration.json": canonical_json(manifest)},
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=route,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_test_protocol(
                uow, request=request, user_scope_id=context.user_scope_id,
                protocol_id=protocol_id, now=now, definition_ids=definition_ids,
                managed=managed,
            ),
            hooks=hooks,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _validate_test_protocol(
        self, request: CreateTestProtocolRequest, user_scope_id: str
    ) -> None:
        with self.repository.unit_of_work() as uow:
            game_id = self._context_game(uow, request.game_context_id, user_scope_id)
            if request.parent_protocol_id is not None:
                parent = self._require_visible_protocol(
                    uow, request.parent_protocol_id, user_scope_id
                )
                if parent.game_id != game_id:
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        "the parent protocol belongs to another game",
                        field_errors=[FieldError(
                            path="parent_protocol_id", code="reference",
                            message="cross-game parent protocol",
                        )],
                    )
        self._validate_conditions(request)
        self._validate_controlled(request)
        if request.metric_registry_version != metrics.REGISTRY_VERSION:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the requested metric registry version is not available",
                field_errors=[FieldError(
                    path="metric_registry_version", code="enum",
                    message="no packaged manifest for this version",
                )],
            )
        if not metrics.RULES:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the packaged metric catalog is empty; no rule can be chosen",
            )
        if not request.metric_keys:
            raise _invalid(
                "metric_keys", "required", "at least one metric key is required"
            )
        if len(set(request.metric_keys)) != len(request.metric_keys):
            raise _invalid(
                "metric_keys", "conflict", "a metric key is listed more than once"
            )
        unknown = [key for key in request.metric_keys if metrics.rule_for(key) is None]
        if unknown:
            raise _invalid(
                "metric_keys", "enum", f"unknown metric keys: {unknown}"
            )

    def _write_test_protocol(
        self,
        uow: UnitOfWork,
        *,
        request: CreateTestProtocolRequest,
        user_scope_id: str,
        protocol_id: str,
        now: str,
        definition_ids: Mapping[str, str],
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        game_id = self._context_game(uow, request.game_context_id, user_scope_id)
        parent: TestProtocol | None = None
        if request.parent_protocol_id is not None:
            parent = self._require_visible_protocol(
                uow, request.parent_protocol_id, user_scope_id
            )
            if parent.game_id != game_id:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the parent protocol belongs to another game",
                )
            series_id = parent.series_id
            latest = uow.latest_for_series("TestProtocol", series_id)
            version = (latest.version if latest is not None else parent.version) + 1
        else:
            series_id = _deterministic_id(user_scope_id, protocol_id, "series")
            version = 1

        selected = [metrics.rule_for(key) for key in request.metric_keys]
        registered = self._register_definitions(
            uow, [rule for rule in selected if rule is not None], now
        )
        if registered != {key: definition_ids[key] for key in registered}:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR,
                "the metric definition identity changed inside one transaction",
            )

        evidence = _declaration_evidence(managed[0])
        conditions, _ = _build_run_conditions(request.condition_fields, evidence, now)
        scenario = (
            _known(str, request.scenario, evidence, now)
            if request.scenario is not None else _unknown(str)
        )
        protocol = TestProtocol(
            id=protocol_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            series_id=series_id,
            version=version,
            game_id=game_id,
            scenario=scenario,
            conditions=conditions,
            controlled_variables=list(request.controlled_variables),
            metric_definition_ids=[
                definition_ids[key] for key in request.metric_keys
            ],
            warmup_seconds=request.warmup_seconds,
            sample_duration_seconds=request.sample_duration_seconds,
            repeat_count=request.repeat_count,
            note=request.note,
            parent_protocol_id=request.parent_protocol_id,
            declaration_file_id=managed[0].id,
        )
        uow.insert_versioned("TestProtocol", protocol)
        uow.insert_reference(protocol.id, "game_context", ObjectRef(
            entity_type="GameContext", entity_id=request.game_context_id,
        ))
        if parent is not None:
            uow.insert_reference(protocol.id, "parent", ObjectRef(
                entity_type="TestProtocol", entity_id=parent.id,
            ))
        for definition_id in protocol.metric_definition_ids:
            uow.insert_reference(protocol.id, "metric_definition", ObjectRef(
                entity_type="MetricDefinition", entity_id=definition_id,
            ))
        return [
            ObjectRef(entity_type="TestProtocol", entity_id=protocol.id),
            ObjectRef(entity_type="ManagedFile", entity_id=managed[0].id),
        ]

    def _register_definitions(
        self, uow: UnitOfWork, rules: Sequence[metrics.MetricRule], now: str
    ) -> dict[str, str]:
        """Get or create one immutable definition per (rule key, version, hash).

        ``BEGIN IMMEDIATE`` serialises the read-then-insert, so two concurrent
        different keys share the definition the first one registered.
        """
        ids: dict[str, str] = {}
        for rule in rules:
            definition_id = _metric_definition_id(rule)
            found = uow.find_metric_rule(
                metric_key=rule.metric_key,
                registry_version=metrics.REGISTRY_VERSION,
                rule_hash=rule.rule_hash,
            )
            if found is not None:
                if found != definition_id or uow.find_object(
                    "MetricDefinition", found
                ) is None:
                    raise ContractError(
                        ErrorCode.INTERNAL_ERROR,
                        "the metric definition index disagrees with the stored definition",
                    )
            else:
                uow.insert_object("MetricDefinition", MetricDefinition(
                    id=definition_id,
                    schema_version=PRODUCT_CONTRACT_VERSION,
                    created_at=now,
                    created_by_task_id=None,
                    metric_key=rule.metric_key,
                    definition_version=rule.definition_version,
                    unit=rule.unit,
                    algorithm_ref=rule.algorithm_ref,
                    frame_stream_kind=rule.frame_stream_kind,
                    minimum_sample_rule=rule.minimum_sample_rule,
                ))
                uow.record_metric_rule(
                    metric_key=rule.metric_key,
                    registry_version=metrics.REGISTRY_VERSION,
                    rule_hash=rule.rule_hash,
                    definition_id=definition_id,
                )
            ids[rule.key] = definition_id
        return ids

    def _validate_conditions(self, request: CreateTestProtocolRequest) -> None:
        _validate_condition_fields(
            request.condition_fields, "condition_fields", allow_scenario=False
        )

    def _validate_controlled(self, request: CreateTestProtocolRequest) -> None:
        fields = request.condition_fields
        seen: set[str] = set()
        for name in request.controlled_variables:
            if name in seen:
                raise _invalid(
                    "controlled_variables", "conflict",
                    f"{name!r} is listed more than once",
                )
            seen.add(name)
            if name == "scenario":
                declared = request.scenario is not None
            elif name in ("output_resolution", "render_resolution"):
                prefix = name.split("_")[0]
                declared = f"{prefix}_width" in fields and f"{prefix}_height" in fields
            elif name in ("frame_generation", "upscaling"):
                declared = name in fields
            elif name.startswith("scene.") and name[len("scene."):].strip():
                declared = name in fields
            else:
                declared = False
            if not declared:
                raise _invalid(
                    "controlled_variables", "reference",
                    f"{name!r} is not a complete, declared condition of this protocol",
                )

    def _context_game(
        self, uow: UnitOfWork, context_id: str, user_scope_id: str
    ) -> str:
        stored = uow.find_object("GameContext", context_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced game context does not exist",
                field_errors=[FieldError(
                    path="game_context_id", code="reference", message="unknown context",
                )],
            )
        payload = stored.payload
        installation_id = payload.get("installation_id")
        if installation_id is not None:
            installation = uow.latest_for_series("GameInstallation", installation_id)
            if installation is None or (
                installation.payload.get("user_scope_id") != user_scope_id
            ):
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the game context is not available"
                )
        game_id = payload.get("game_id")
        if not isinstance(game_id, str):
            raise ContractError(
                ErrorCode.REFERENCE_INVALID, "the game context names no game"
            )
        return game_id

    def _peek_game_id(self, context_id: str) -> str | None:
        with self.repository.unit_of_work() as uow:
            stored = uow.find_object("GameContext", context_id)
        if stored is None:
            return None
        value = stored.payload.get("game_id")
        return value if isinstance(value, str) else None

    def _require_visible_protocol(
        self, uow: UnitOfWork, protocol_id: str, user_scope_id: str
    ) -> TestProtocol:
        """A parent must be same-game *and* visible (04 §4.4).

        Visibility is resolved from the protocol's own game context: an
        installation-owned context belongs to one user, so another user's
        protocol is indistinguishable from a missing one.
        """
        parent = self._load_test_protocol(uow, protocol_id)
        refs = uow.query(
            "SELECT target_id FROM object_references"
            " WHERE owner_id=? AND relation='game_context'",
            (parent.id,),
        )
        if not refs:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the parent protocol records no owning context; its visibility"
                " cannot be verified",
            )
        self._context_game(uow, refs[0]["target_id"], user_scope_id)
        return parent

    def _load_test_protocol(self, uow: UnitOfWork, protocol_id: str) -> TestProtocol:
        stored = uow.find_object("TestProtocol", protocol_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced test protocol does not exist",
            )
        return TestProtocol.model_validate_json(stored.payload_bytes)

    # -- historical log preview and association (04 §4.3) ------------------
    def inspect_log(
        self, file_id: str, source_schema: str, user_scope_id: str
    ) -> LogInspection:
        """Bounded, read-only preview: no session, artifact or model call."""
        stored = self.repository.get_object("ManagedFile", file_id)
        payload = stored.payload
        self._require_file_owner(payload, user_scope_id)
        if payload.get("file_status") != "published":
            raise ContractError(
                ErrorCode.ARTIFACT_UNAVAILABLE, "the log file is not consumable"
            )
        content_hash = payload["manifest"]["sha256"]
        data = self.files.read_managed_file(file_id)
        parsed = parse_log(
            data,
            registry=self.log_registry,
            evidence=_artifact_evidence(payload),
        )
        if parsed.format_id != source_schema:
            raise ContractError(
                ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
                "the file does not match the named source schema; its original is retained",
            )
        return LogInspection(
            file_id=file_id,
            content_hash=content_hash,
            source_schema=source_schema,
            source_ranges=parsed.source_ranges,
            streams=_stream_candidates(parsed, content_hash),
            limitations=list(parsed.limitations),
        )

    def associate_log(
        self,
        request: LogAssociationRequest,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        route = LOG_ASSOCIATION_ROUTE
        request_hash = _request_hash(route, request_schema_version, request)
        ids = {
            role: _deterministic_id(
                context.user_scope_id, route, context.idempotency_key, request_hash, role,
            )
            for role in ("artifact", "session", "segment", "binding", "declaration")
        }
        if self._has_registered(context, route):
            # A replay returns its original response; the placeholder bytes are
            # never written because the idempotency record is found first.
            published = self.files.publish(
                kind="import",
                files={f"log-associations/{ids['artifact']}/declaration.json": b"{}"},
                user_scope_id=context.user_scope_id,
                method="POST",
                canonical_route=route,
                key=context.idempotency_key,
                request_schema_version=request_schema_version,
                request_hash=request_hash,
                hooks=hooks,
            )
            return ResourceResult(
                refs=[ObjectRef.model_validate(ref) for ref in published.refs]
            )

        plan = self._prepare_association(request, context.user_scope_id, ids)
        manifest = {
            "kind": "log_association",
            "rule_version": LOG_ASSOCIATION_RULE_VERSION,
            "input": request.model_dump(mode="json"),
            "artifact_id": ids["artifact"],
            "bindings": [plan["binding_ref"]],
            "session_revision": plan["session_revision"],
            # The mapping's checks carry a checked_at; only the deterministic
            # derived values may enter a frozen file (same-key retry).
            "time_mapping": _mapping_values(plan["time_mapping"]),
        }
        published = self.files.publish(
            kind="import",
            files={
                f"log-associations/{ids['artifact']}/declaration.json":
                    canonical_json(manifest)
            },
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=route,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_association(
                uow, request=request, user_scope_id=context.user_scope_id,
                parsed=plan["parsed"], time_mapping=plan["time_mapping"],
                ids=ids, managed=managed,
            ),
            hooks=hooks,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _prepare_association(
        self, request: LogAssociationRequest, user_scope_id: str, ids: Mapping[str, str]
    ) -> dict[str, Any]:
        stored = self.repository.get_object("ManagedFile", request.file_id)
        payload = stored.payload
        self._require_file_owner(payload, user_scope_id)
        if payload.get("file_status") != "published":
            raise ContractError(
                ErrorCode.ARTIFACT_UNAVAILABLE, "the log file is not consumable"
            )
        if payload["manifest"]["sha256"] != request.content_hash:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the submitted content hash does not match the stored log",
                field_errors=[FieldError(
                    path="content_hash", code="reference",
                    message="the file changed or was named with another hash",
                )],
            )
        data = self.files.read_managed_file(request.file_id)
        parsed = parse_log(
            data,
            registry=self.log_registry,
            evidence=_artifact_evidence(payload),
        )
        if parsed.format_id != request.source_schema:
            raise ContractError(
                ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
                "the file does not match the named source schema; its original is retained",
            )
        candidates = _stream_candidates(parsed, request.content_hash)
        chosen = next(
            (item for item in candidates if item.stream_key == request.stream_key), None
        )
        if chosen is None:
            raise _invalid(
                "stream_key", "reference",
                "the stream key does not exist in the inspected log",
            )
        self._require_interpretable_range(parsed, request)
        scale = parsed.time_mapping.scale
        if scale is None:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the log's time unit was not verified; the clock mapping is not guessed",
            )
        offset = request.target_anchor_s - request.source_anchor * scale
        time_mapping = TimeMapping(
            source_basis=parsed.time_mapping.source_basis,
            target_basis=LOG_TARGET_TIME_BASIS,
            source_unit=parsed.time_mapping.source_unit,
            target_unit="s",
            offset=offset,
            scale=scale,
            checks=[
                _check(
                    "time.anchor", CheckStatus.pass_,
                    "the mapping is derived from the player-supplied anchors; it is"
                    " user_provided, not an automatic observation", self.clock(),
                ),
                _check(
                    "time.observation", CheckStatus.not_run,
                    "no automatic clock observation was made", self.clock(),
                ),
            ],
        )
        with self.repository.unit_of_work() as uow:
            plan = self._association_plan(
                uow, request, user_scope_id, parsed, time_mapping, ids
            )
        plan["parsed"] = parsed
        plan["time_mapping"] = time_mapping
        plan["chosen"] = chosen
        plan["content_hash"] = request.content_hash
        return plan

    def _association_plan(
        self,
        uow: UnitOfWork,
        request: LogAssociationRequest,
        user_scope_id: str,
        parsed: LogParseResult,
        time_mapping: TimeMapping,
        ids: Mapping[str, str],
    ) -> dict[str, Any]:
        self._evidence_problems(uow, request.evidence_refs, user_scope_id)
        if not request.evidence_refs:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "an association needs evidence for its anchor and range",
            )
        target = request.target
        scale = time_mapping.scale or 0.0
        mapped_start = (request.source_range.start - request.source_anchor) * scale
        mapped_start += request.target_anchor_s
        mapped_end = (request.source_range.end - request.source_anchor) * scale
        mapped_end += request.target_anchor_s

        if isinstance(target, HistoricalLogTarget):
            installation = self._installation(uow, target.installation_id, user_scope_id)
            # Visibility first: a foreign context is RESOURCE_NOT_FOUND, never a
            # reference error that confirms the id exists.
            self._context_game(uow, target.game_context_id, user_scope_id)
            self._context(uow, target.game_context_id, installation)
            self._device_snapshot(uow, target.device_snapshot_id, user_scope_id)
            self._require_declaration_installation(
                target.run_declaration, installation["series_id"]
            )
            self._test_protocol_ref(uow, target.test_protocol_id, installation["game_id"])
            if (
                request.source_anchor != request.source_range.start
                or request.target_anchor_s != 0
            ):
                raise _invalid(
                    "target", "conflict",
                    "a historical session anchor is the range start mapped to second 0",
                )
            try:
                started = datetime.fromisoformat(target.started_at)
                now = datetime.fromisoformat(self.clock())
            except ValueError as error:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "the historical start is not a usable backend time",
                ) from error
            if started > now:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "a historical start cannot lie in the future",
                )
            duration = (request.source_range.end - request.source_range.start) * scale
            if not math.isfinite(duration) or duration <= 0:
                raise _invalid(
                    "source_range", "range", "the source range yields no positive duration"
                )
            if started + timedelta(seconds=duration) > now:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "the log's end would lie in the future; the start or range is not"
                    " a verified historical fact",
                )
            return {
                "historical": True,
                "session_id": ids["session"],
                "segment_id": ids["segment"],
                "session_revision": 1,
                "binding_ref": {
                    "segment_id": ids["segment"],
                    "segment_revision": 1,
                    "binding_revision": 1,
                },
                "close_at_s": None,
                "is_open": False,
                "duration_s": duration,
                "started_at": target.started_at,
                "mapped_start": mapped_start,
                "mapped_end": mapped_end,
            }

        session = self._load_session(uow, target.session_id)
        self._require_session_owner(uow, session, user_scope_id)
        if session.revision != target.expected_session_revision:
            raise ContractError(
                ErrorCode.BINDING_REVISION_CONFLICT,
                "the session changed; re-read it before associating the log",
                field_errors=[FieldError(
                    path="session_revision", code="conflict",
                    message=f"current revision is {session.revision}",
                )],
                details=empty_details(
                    expected_revision=target.expected_session_revision,
                    current_revision=session.revision,
                    current_resource_ref=ObjectRef(
                        entity_type="PlaySession", entity_id=session.id,
                    ),
                ),
            )
        segment = self._load_segment(uow, target.segment_ref.segment_id)
        if segment.session_id != session.id:
            # Resolve the owning session first: another user's segment must not
            # be distinguishable from a missing one.
            owner_session = self._load_session(uow, segment.session_id)
            self._require_session_owner(uow, owner_session, user_scope_id)
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the target segment belongs to another session",
            )
        self._require_current_revisions(
            segment,
            expected_segment_revision=target.segment_ref.segment_revision,
            expected_binding_revision=target.segment_ref.binding_revision,
        )
        if segment.lifecycle is SessionLifecycle.ended:
            if target.close_at_s is not None:
                raise _invalid(
                    "target.close_at_s", "conflict",
                    "an ended segment keeps its boundary; close_at_s must be null",
                )
            lower, upper = segment.start_s, segment.end_s
            binding_revision = segment.binding_revision
            segment_revision = segment.revision
            is_open = False
        else:
            if session.lifecycle is not SessionLifecycle.active:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "an open segment in an ended session cannot be associated",
                )
            if target.close_at_s is None:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "an open segment needs a finite end point with its evidence",
                )
            if (
                not math.isfinite(target.close_at_s)
                or target.close_at_s <= segment.start_s
            ):
                raise _invalid(
                    "target.close_at_s", "range",
                    "the close point must be after the segment start",
                )
            if target.close_at_s > self._elapsed_seconds(session, segment):
                raise _invalid(
                    "target.close_at_s", "range",
                    "the close point lies in the future; only an actual end point may"
                    " close the segment",
                )
            lower, upper = segment.start_s, target.close_at_s
            binding_revision = segment.binding_revision + 1
            segment_revision = segment.revision + 1
            is_open = True
        if (
            not math.isfinite(mapped_start)
            or not math.isfinite(mapped_end)
            or mapped_start < 0
            or mapped_start < lower
            or mapped_end > upper
        ):
            raise _invalid(
                "source_range", "range",
                "the mapped range must be non-negative and inside the target segment",
            )
        return {
            "historical": False,
            "session_id": session.id,
            "segment_id": segment.id,
            "session_revision": session.revision,
            "binding_ref": {
                "segment_id": segment.id,
                "segment_revision": segment_revision,
                "binding_revision": binding_revision,
            },
            "close_at_s": target.close_at_s,
            "is_open": is_open,
            "duration_s": None,
            "started_at": None,
            "mapped_start": mapped_start,
            "mapped_end": mapped_end,
        }

    def _require_interpretable_range(
        self, parsed: LogParseResult, request: LogAssociationRequest
    ) -> None:
        if not parsed.source_ranges:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the log states no verified source range; the original is retained",
            )
        wanted = request.source_range
        inside = any(
            candidate.time_basis == wanted.time_basis
            and candidate.unit == wanted.unit
            and wanted.start >= candidate.start
            and wanted.end <= candidate.end
            for candidate in parsed.source_ranges
        )
        if not inside:
            raise _invalid(
                "source_range", "range",
                "the source range lies outside the log's interpretable range",
            )
        if not (wanted.start <= request.source_anchor <= wanted.end):
            raise _invalid(
                "source_anchor", "range",
                "the source anchor must lie inside the submitted source range",
            )

    def _write_association(
        self,
        uow: UnitOfWork,
        *,
        request: LogAssociationRequest,
        user_scope_id: str,
        parsed: LogParseResult,
        time_mapping: TimeMapping,
        ids: Mapping[str, str],
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        # Re-check every revision inside the write transaction: the plan was
        # prepared outside it and a concurrent change must lose, not merge.
        plan = self._association_plan(
            uow, request, user_scope_id, parsed, time_mapping, ids
        )
        now = self.clock()
        target = request.target
        # Re-read and re-hash the source inside the writing transaction: the
        # parse ran outside it and the bytes may have changed since (04 §4.3).
        self._verify_source_bytes(uow, request.file_id, request.content_hash)
        declaration_evidence = _declaration_evidence(managed[0])
        evidence = declaration_evidence
        stream_checks = [
            _check(
                "segment.stream", CheckStatus.pass_,
                "the selected stream is the one the player named from the preview", now,
            ),
        ]
        chosen = next(
            item for item in _stream_candidates(parsed, request.content_hash)
            if item.stream_key == request.stream_key
        )
        selection = StreamSelection(
            process=chosen.process,
            swapchain_id=chosen.swapchain_id,
            frame_stream_kind=chosen.frame_stream_kind,
            checks=stream_checks,
        )

        if isinstance(target, HistoricalLogTarget) and plan["historical"]:
            conditions, scenario = _build_run_conditions(
                target.run_declaration.fields, evidence, now
            )
            started = datetime.fromisoformat(plan["started_at"])
            ended_at = (started + timedelta(seconds=plan["duration_s"])).isoformat()
            declaration = PlayerDeclaration(
                id=ids["declaration"],
                schema_version=PRODUCT_CONTRACT_VERSION,
                created_at=now,
                created_by_task_id=None,
                installation_id=target.installation_id,
                input=ContextDeclarationInput(
                    kind="context_declaration",
                    installation_id=target.installation_id,
                    game_id=self._context_game(uow, target.game_context_id, user_scope_id),
                    fields=target.run_declaration.fields,
                ),
                raw_text=target.run_declaration.raw_text,
                submitted_at=now,
                declaration_file_id=managed[0].id,
            )
            session = PlaySession(
                id=ids["session"],
                schema_version=PRODUCT_CONTRACT_VERSION,
                created_at=now,
                created_by_task_id=None,
                installation_id=target.installation_id,
                game_id=declaration.input.game_id,
                device_snapshot_id=target.device_snapshot_id,
                game_context_id=target.game_context_id,
                started_at=target.started_at,
                ended_at=ended_at,
                revision=1,
                lifecycle=SessionLifecycle.ended,
                initial_applied_id=None,
                recommendation_id=None,
                configuration_state=ConfigurationState.unverified,
                scenario=scenario,
                target_proposal_ref=None,
                test_protocol_id=target.test_protocol_id,
                run_conditions=conditions,
            )
            segment = SessionSegment(
                id=ids["segment"],
                schema_version=PRODUCT_CONTRACT_VERSION,
                created_at=now,
                created_by_task_id=None,
                session_id=session.id,
                start_s=0.0,
                end_s=plan["duration_s"],
                time_basis=SESSION_TIME_BASIS,
                revision=1,
                lifecycle=SessionLifecycle.ended,
                applied_id=None,
                configuration_state=ConfigurationState.unverified,
                binding_revision=1,
                scenario=scenario,
                excluded_ranges=[],
                stream_selection=selection,
                run_conditions=conditions,
            )
            binding = ConfigurationBinding(
                id=ids["binding"],
                schema_version=PRODUCT_CONTRACT_VERSION,
                created_at=now,
                created_by_task_id=None,
                segment_id=segment.id,
                binding_revision=1,
                applied_id=None,
                configuration_state=ConfigurationState.unverified,
                effective_range=BindingRange(
                    start=0.0, end=plan["duration_s"],
                    time_basis=SESSION_TIME_BASIS, unit="s",
                ),
                evidence_refs=[],
                supersedes_revision=None,
            )
            uow.insert_object("PlayerDeclaration", declaration)
            uow.insert_object("PlaySession", session)
            uow.insert_object("SessionSegment", segment)
            uow.insert_object("ConfigurationBinding", binding)
            uow.append_snapshot("PlaySession", session.id, session)
            uow.append_snapshot("SessionSegment", segment.id, segment)
            self._insert_segment_head(uow, segment, segment.id, now)
            self._insert_binding_row(uow, binding, now)
            installation = self._installation(
                uow, target.installation_id, user_scope_id
            )
            context = self._context(uow, target.game_context_id, installation)
            self._write_creation_references(
                uow, session=session, segment=segment, declaration=declaration,
                installation=installation, context=context,
            )
            uow.record_domain_event(
                target_type="PlaySession", target_id=session.id, dimension="session",
                sequence=1, previous_state=None, next_state=SessionLifecycle.ended.value,
                cause="historical_log_registered",
                related_refs=[
                    ObjectRef(entity_type="PlayerDeclaration", entity_id=declaration.id),
                ],
                created_at=now,
            )
            uow.record_domain_event(
                target_type="SessionSegment", target_id=segment.id, dimension="session",
                sequence=1, previous_state=None, next_state=SessionLifecycle.ended.value,
                cause="historical_log_registered",
                related_refs=[ObjectRef(entity_type="PlaySession", entity_id=session.id)],
                created_at=now,
            )
        else:
            session = self._load_session(uow, plan["session_id"])
            segment = self._load_segment(uow, plan["segment_id"])
            if plan["is_open"]:
                closed = self._close_segment_at(
                    uow, segment, session, plan["close_at_s"], now=now,
                    evidence_refs=list(request.evidence_refs),
                )
                updated = segment.model_copy(update={
                    "revision": segment.revision + 1,
                    "end_s": plan["close_at_s"],
                    "lifecycle": SessionLifecycle.ended,
                    "binding_revision": closed.binding_revision,
                    "stream_selection": selection,
                })
                self._write_segment_revision(
                    uow, segment, updated, session,
                    cause="segment_closed_for_log_association",
                )
        artifact = CaptureArtifact(
            id=ids["artifact"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            session_id=plan["session_id"],
            segment_ids=[plan["segment_id"]],
            file_id=request.file_id,
            tool_name=parsed.tool_name,
            tool_version=parsed.tool_version,
            options={},
            source_schema=parsed.source_schema,
            source_column_map=list(parsed.column_map),
            time_mapping=time_mapping,
            quality=parsed.quality,
            checks=list(parsed.checks) + [
                _check_with_evidence(
                    "log.association", CheckStatus.pass_,
                    "the artifact maps the source clock with the player-supplied anchors;"
                    " the correspondence is user_provided, not controlled", now,
                    [declaration_evidence],
                ),
                _check(
                    "log.anchor_observation", CheckStatus.not_run,
                    "no automatic clock observation was made", now,
                ),
                _check_with_evidence(
                    "log.evidence", CheckStatus.pass_,
                    f"{len(request.evidence_refs)} evidence reference(s) back the anchor"
                    " and range", now,
                    list(request.evidence_refs) + [declaration_evidence],
                ),
            ],
        )
        uow.insert_object("CaptureArtifact", artifact)
        uow.insert_reference(artifact.id, "session", ObjectRef(
            entity_type="PlaySession", entity_id=artifact.session_id,
        ))
        uow.insert_reference(artifact.id, "segment", ObjectRef(
            entity_type="SessionSegment", entity_id=artifact.segment_ids[0],
        ))
        uow.insert_reference(artifact.id, "file", ObjectRef(
            entity_type="ManagedFile", entity_id=artifact.file_id,
        ))
        return [
            ObjectRef(entity_type="CaptureArtifact", entity_id=artifact.id),
            ObjectRef(entity_type="ManagedFile", entity_id=managed[0].id),
        ]

    # -- deterministic analysis (04 §5/§6) ----------------------------------
    def analyze(self, request: Any, context: MutationContext) -> TaskAccepted:
        """Submit an analysis task; pure conflicts are refused before the key.

        The guards run *before* ``register_task`` so an open revision or a
        missing original never consumes the caller's idempotency key
        (error-conventions §4.2).
        """
        task_input = self._analysis_input(request)
        self._load_analysis_targets(task_input, context.user_scope_id)
        if self.tasks is None:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR, "the analysis handler is not wired"
            )
        return self.tasks.register_task(task_input, context)

    def _analysis_input(self, request: Any) -> AnalysisInput:
        if isinstance(request, AnalysisInput):
            return request
        try:
            return AnalysisInput.model_validate_json(canonical_json(request))
        except Exception as error:  # pydantic ValidationError, no writes yet
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the analysis input is invalid",
                field_errors=[FieldError(
                    path="input", code="type", message=str(error).splitlines()[0],
                )],
            ) from error

    def _load_artifact(self, uow: UnitOfWork, artifact_id: str) -> CaptureArtifact:
        stored = uow.find_object("CaptureArtifact", artifact_id)
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested artifact is not available"
            )
        return CaptureArtifact.model_validate_json(stored.payload_bytes)

    def _load_analysis_targets(
        self, request: AnalysisInput, user_scope_id: str
    ) -> "_AnalysisTargets":
        if request.analyzer_version != ANALYZER_VERSION:
            raise _invalid(
                "analyzer_version", "enum",
                "the requested analyzer version is not the packaged one",
            )
        if request.cleaning_protocol_version != CLEANING_PROTOCOL_VERSION:
            raise _invalid(
                "cleaning_protocol_version", "enum",
                "the requested cleaning version is not the packaged one",
            )
        if not request.bindings:
            raise _invalid(
                "bindings", "required", "an analysis names at least one closed binding"
            )
        keys = [
            (item.segment_id, item.segment_revision, item.binding_revision)
            for item in request.bindings
        ]
        if len(set(keys)) != len(keys):
            raise _invalid("bindings", "conflict", "a binding is listed more than once")

        with self.repository.unit_of_work() as uow:
            artifact = self._load_artifact(uow, request.artifact_id)
            session = self._load_session(uow, artifact.session_id)
            # Visibility first: another user's artifact must be indistinguishable
            # from a missing one, and the backing original needs an owner too.
            self._require_session_owner(uow, session, user_scope_id)
            file_row = uow.find_object("ManagedFile", artifact.file_id)
            if file_row is None:
                raise ContractError(
                    ErrorCode.ARTIFACT_UNAVAILABLE,
                    "the artifact's original is not registered",
                )
            file_payload = file_row.payload
            if file_payload.get("user_scope_id") != user_scope_id:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested artifact is not available"
                )
            if file_payload.get("file_status") != "published":
                raise ContractError(
                    ErrorCode.ARTIFACT_UNAVAILABLE,
                    "the artifact's original is not consumable",
                )
            content_hash = file_payload["manifest"]["sha256"]

        for binding_ref in request.bindings:
            self.require_analyzeable(binding_ref, user_scope_id)

        bindings: list[tuple[SessionSegment, ConfigurationBinding]] = []
        with self.repository.unit_of_work() as uow:
            for binding_ref in request.bindings:
                segment = self._load_segment_revision(
                    uow, binding_ref.segment_id, binding_ref.segment_revision
                )
                binding = self._load_binding(
                    uow, segment.id, binding_ref.binding_revision
                )
                if segment.session_id != artifact.session_id:
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        "the artifact and the binding name different sessions",
                    )
                if segment.id not in artifact.segment_ids:
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        "the artifact does not cover the named segment",
                    )
                if binding.effective_range.end is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "an open binding cannot be analyzed",
                    )
                bindings.append((segment, binding))

        # The managed read re-verifies the bytes against the manifest hash; a
        # replaced original is never silently analyzed.
        data = self.files.read_managed_file(artifact.file_id)
        if hashlib.sha256(data).hexdigest() != content_hash:
            raise ContractError(
                ErrorCode.ARTIFACT_UNAVAILABLE,
                "the artifact's original no longer matches its recorded hash",
            )
        return _AnalysisTargets(
            artifact=artifact,
            session=session,
            bindings=bindings,
            file_payload=file_payload,
            data=data,
        )

    def handle_analysis(self, context: TaskContext) -> HandlerOutcome:
        """Task handler: parse, clean, summarize and publish a new summary."""
        task_input = context.task_input
        if not isinstance(task_input, AnalysisInput):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the analysis handler received another task type",
            )
        context.phase("analyze")
        targets = self._load_analysis_targets(task_input, context.user_scope_id)
        plan = self._prepare_analysis(task_input, targets)
        context.phase("finalize")
        published = self._publish_analysis(
            task_input, targets, plan,
            user_scope_id=context.user_scope_id,
            task_id=context.task_id,
        )
        refs = [ObjectRef.model_validate(item) for item in published.refs]
        state = (
            TaskState.partial
            if plan.quality is LogQuality.invalid
            else TaskState.completed
        )
        return HandlerOutcome(
            state=state,
            domain_result=DomainResult(
                domain=TaskType.analysis,
                code=plan.quality.value,
                input_revision=context.task_input_revision,
                checks=list(plan.checks),
            ),
            result_refs=refs,
        )

    def _prepare_analysis(
        self, request: AnalysisInput, targets: "_AnalysisTargets"
    ) -> "_AnalysisPlan":
        parsed = parse_log(
            targets.data,
            registry=self.log_registry,
            evidence=_artifact_evidence(targets.file_payload),
        )
        if parsed.format_id != targets.artifact.source_schema:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the original no longer matches the artifact's verified schema",
            )
        mapping = targets.artifact.time_mapping
        if mapping.scale is None or mapping.offset is None:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the artifact has no verified time mapping; its frames cannot be placed"
                " in the session",
            )

        analyses: list[_BindingCleaning] = []
        for segment, binding in targets.bindings:
            window = _analysis_window(segment, binding)
            cleaning = clean_frames(
                parsed.frames,
                time_mapping=mapping,
                window=window,
                excluded_ranges=segment.excluded_ranges,
            )
            analyses.append(_BindingCleaning(
                segment=segment, binding=binding, window=window, cleaning=cleaning
            ))

        stream_problem = _unconfirmed_stream_reason(parsed)
        confirmed = stream_problem is None
        stream_reason = None if stream_problem is None else stream_problem[0]
        stream_unknown = "not_provided" if stream_problem is None else stream_problem[1]
        # Two candidates are two streams: reporting their merged intervals would
        # invent a stream nobody selected, so nothing is aggregated at all.
        mergeable = len(parsed.observed_streams) <= 1
        intervals = (
            [interval for item in analyses for interval in item.cleaning.intervals]
            if mergeable else []
        )
        valid_count = len(intervals)
        duration = sum(intervals) / 1000.0

        if valid_count == 0:
            quality = LogQuality.invalid
        else:
            quality = quality_for_sample(valid_count, duration)
            quality = LogQuality(quality)
            if not confirmed:
                quality = _worse_quality(quality, LogQuality.limited)
        quality = _worse_quality(quality, parsed.quality)

        now = self.clock()
        artifact_ref = ObjectRef(
            entity_type="CaptureArtifact", entity_id=targets.artifact.id
        )
        metric_values = [
            _metric_value(
                rule,
                intervals=intervals,
                confirmed=confirmed,
                stream_unknown=stream_unknown,
                stream_kind=parsed.frame_stream_kind,
                artifact_ref=artifact_ref,
            )
            for rule in metrics.RULES
        ]

        adaptive: list[AdaptiveLongFrame] = []
        if mergeable:
            for item in analyses:
                for run in item.cleaning.runs:
                    adaptive.append(_adaptive_long_frame(run, item.segment))

        checks = [
            _analysis_check(
                "analysis.artifact", CheckStatus.pass_,
                "the original hash and the artifact's schema were re-verified", now,
            ),
            _analysis_check(
                "analysis.bindings", CheckStatus.pass_,
                f"{len(analyses)} named closed binding revision(s) were read", now,
            ),
            _analysis_check(
                "analysis.stream",
                CheckStatus.pass_ if confirmed else CheckStatus.unknown,
                stream_reason or "the frame stream is confirmed as application_present",
                now,
            ),
            _analysis_check(
                "analysis.cleaning",
                CheckStatus.pass_ if valid_count else CheckStatus.unknown,
                f"{valid_count} valid interval(s) in {len(adaptive)} continuous run(s);"
                f" {sum(len(item.cleaning.breaks) for item in analyses)} break(s);"
                " spikes are retained and nothing is reconnected across a break",
                now,
            ),
            _analysis_check(
                "analysis.sample",
                CheckStatus.pass_ if quality is LogQuality.valid else CheckStatus.unknown,
                f"effective duration {duration:.6f}s with {valid_count} interval(s);"
                " the project's 30s/1000-interval gate decides whether the summary is"
                " limited",
                now,
            ),
        ]

        manifest = {
            "kind": "analysis_manifest",
            "rule_version": ANALYSIS_RULE_VERSION,
            "analyzer_version": ANALYZER_VERSION,
            "cleaning_protocol_version": CLEANING_PROTOCOL_VERSION,
            "artifact_id": targets.artifact.id,
            "artifact_file_id": targets.artifact.file_id,
            "source_schema": targets.artifact.source_schema,
            "source_hash": targets.file_payload["manifest"]["sha256"],
            "stream_kind": parsed.frame_stream_kind.value,
            "stream_reason": stream_reason,
            "bindings": [
                {
                    "ref": {
                        "segment_id": item.binding.segment_id,
                        "segment_revision": item.segment.revision,
                        "binding_revision": item.binding.binding_revision,
                    },
                    "analyzed_range": item.window.model_dump(mode="json"),
                    "cleaning": cleaning_manifest(
                        item.cleaning,
                        mapping=mapping,
                        column_map=targets.artifact.source_column_map,
                    ),
                }
                for item in analyses
            ],
            "valid_interval_count": valid_count,
            "effective_duration_s": duration,
            "quality": quality.value,
            "metrics": [item.model_dump(mode="json") for item in metric_values],
            "adaptive_long_frames": [
                item.model_dump(mode="json") for item in adaptive
            ],
        }
        return _AnalysisPlan(
            metrics=metric_values,
            adaptive=adaptive,
            quality=quality,
            checks=checks,
            manifest=manifest,
            analyzed_ranges=[item.window for item in analyses],
            valid_interval_count=valid_count,
            effective_duration_s=duration,
        )

    def _publish_analysis(
        self,
        request: AnalysisInput,
        targets: "_AnalysisTargets",
        plan: "_AnalysisPlan",
        *,
        user_scope_id: str,
        task_id: str,
    ):
        manifest_bytes = canonical_json(plan.manifest)
        summary_id = _deterministic_id(user_scope_id, task_id, "summary")
        now = self.clock()
        published = self.files.publish(
            kind="import",
            files={f"analyses/{task_id}/manifest.json": manifest_bytes},
            user_scope_id=user_scope_id,
            method="POST",
            canonical_route=f"{ANALYSIS_ROUTE}/{task_id}/manifest",
            key=task_id,
            request_schema_version=PRODUCT_CONTRACT_VERSION,
            request_hash=hashlib.sha256(manifest_bytes).hexdigest(),
            writer=lambda uow, operation, managed: self._write_summary(
                uow,
                summary_id=summary_id,
                task_id=task_id,
                now=now,
                request=request,
                targets=targets,
                plan=plan,
                manifest_file_id=managed[0].id,
            ),
        )
        return published

    def _write_summary(
        self,
        uow: UnitOfWork,
        *,
        summary_id: str,
        task_id: str,
        now: str,
        request: AnalysisInput,
        targets: "_AnalysisTargets",
        plan: "_AnalysisPlan",
        manifest_file_id: str,
    ) -> list[ObjectRef]:
        # The summary references the definitions it used; register them in the
        # same transaction so no dangling definition id is published.
        self._register_definitions(uow, metrics.RULES, now)
        summary = PerformanceSummary(
            id=summary_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=task_id,
            artifact_id=targets.artifact.id,
            bindings=list(request.bindings),
            analyzed_ranges=list(plan.analyzed_ranges),
            analyzer_version=ANALYZER_VERSION,
            cleaning_protocol_version=CLEANING_PROTOCOL_VERSION,
            manifest_file_id=manifest_file_id,
            valid_interval_count=plan.valid_interval_count,
            effective_duration_s=plan.effective_duration_s,
            metrics=list(plan.metrics),
            adaptive_long_frames=list(plan.adaptive),
            quality=plan.quality,
            checks=list(plan.checks),
        )
        uow.insert_object("PerformanceSummary", summary)
        uow.insert_reference(summary.id, "artifact", ObjectRef(
            entity_type="CaptureArtifact", entity_id=targets.artifact.id,
        ))
        for segment, binding in targets.bindings:
            uow.insert_reference(summary.id, "binding", ObjectRef(
                entity_type="ConfigurationBinding", entity_id=binding.id,
            ))
        return [
            ObjectRef(entity_type="PerformanceSummary", entity_id=summary.id),
            ObjectRef(entity_type="ManagedFile", entity_id=manifest_file_id),
        ]

    def get_summary(self, summary_id: str, user_scope_id: str) -> PerformanceSummary:
        with self.repository.unit_of_work() as uow:
            stored = uow.find_object("PerformanceSummary", summary_id)
            if stored is None:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested summary is not available"
                )
            summary = PerformanceSummary.model_validate_json(stored.payload_bytes)
            artifact = self._load_artifact(uow, summary.artifact_id)
            session = self._load_session(uow, artifact.session_id)
            self._require_session_owner(uow, session, user_scope_id)
            return summary

    # -- comparison protocols and comparisons (04 §4.2/§6) ------------------
    def create_comparison_protocol(
        self,
        request: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        route = COMPARISON_PROTOCOL_ROUTE
        typed = self._comparison_protocol_request(request)
        request_hash = _request_hash(route, request_schema_version, typed)
        protocol_id = _deterministic_id(
            context.user_scope_id, route, context.idempotency_key, request_hash,
            "comparison-protocol",
        )
        if self._has_registered(context, route):
            # Replay the original response; never recompute the threshold or the
            # frozen_at of a protocol that already committed.
            published = self.files.publish(
                kind="import",
                files={f"comparison-protocols/{protocol_id}/declaration.json": b"{}"},
                user_scope_id=context.user_scope_id,
                method="POST",
                canonical_route=route,
                key=context.idempotency_key,
                request_schema_version=request_schema_version,
                request_hash=request_hash,
                hooks=hooks,
            )
            return ResourceResult(
                refs=[ObjectRef.model_validate(ref) for ref in published.refs]
            )

        prepared = self._prepare_comparison_protocol(typed, context.user_scope_id)
        manifest = {
            "kind": "comparison_protocol_declaration",
            "rule_version": COMPARISON_PROTOCOL_RULE_VERSION,
            "input": typed.model_dump(mode="json"),
            "protocol_id": protocol_id,
            "threshold_ms": prepared["threshold_ms"],
            "baseline_statistics": prepared["statistics"],
            "metric_registry_version": metrics.REGISTRY_VERSION,
            "registry_manifest_hash": metrics.registry_manifest_hash(),
            "baseline_summary_ids": list(typed.baseline_summary_ids),
            "baseline_binding_refs": [
                item.model_dump(mode="json") for item in typed.baseline_binding_refs
            ],
        }
        now = self.clock()
        published = self.files.publish(
            kind="import",
            files={
                f"comparison-protocols/{protocol_id}/declaration.json":
                    canonical_json(manifest)
            },
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=route,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_comparison_protocol(
                uow, request=typed, protocol_id=protocol_id, now=now,
                threshold_ms=prepared["threshold_ms"], managed=managed,
            ),
            hooks=hooks,
            owner_ref=protocol_id,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _comparison_protocol_request(self, request: Any) -> CreateComparisonProtocolRequest:
        if isinstance(request, CreateComparisonProtocolRequest):
            return request
        try:
            return CreateComparisonProtocolRequest.model_validate_json(
                canonical_json(request)
            )
        except Exception as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the comparison protocol input is invalid",
                field_errors=[FieldError(
                    path="input", code="type", message=str(error).splitlines()[0],
                )],
            ) from error

    def _prepare_comparison_protocol(
        self, request: CreateComparisonProtocolRequest, user_scope_id: str
    ) -> dict[str, Any]:
        if request.cleaning_definition != CLEANING_PROTOCOL_VERSION:
            raise _invalid(
                "cleaning_definition", "enum",
                "the requested cleaning definition is not the packaged version",
            )
        if request.clustering_definition != LONG_FRAME_CLUSTERING_VERSION:
            raise _invalid(
                "clustering_definition", "enum",
                "the requested clustering definition is not the packaged version",
            )
        if request.denominator_definition != LONG_FRAME_DENOMINATOR_VERSION:
            raise _invalid(
                "denominator_definition", "enum",
                "the requested denominator definition is not the packaged version",
            )
        if request.threshold_mode is ThresholdMode.baseline_derived:
            if request.threshold_ms is not None:
                raise _invalid(
                    "threshold_ms", "enum",
                    "a baseline-derived protocol must not self-report a threshold",
                )
            if not request.baseline_summary_ids:
                raise _invalid(
                    "baseline_summary_ids", "required",
                    "a baseline-derived protocol needs its baseline summaries",
                )
            if not request.baseline_binding_refs:
                raise _invalid(
                    "baseline_binding_refs", "required",
                    "a baseline-derived protocol needs its baseline bindings",
                )
        elif request.threshold_ms is not None and (
            not math.isfinite(request.threshold_ms) or request.threshold_ms <= 0
        ):
            raise _invalid("threshold_ms", "range", "a fixed threshold is finite and > 0")

        summaries = self._validate_baselines(request, user_scope_id)
        if request.threshold_mode is ThresholdMode.fixed:
            return {
                "threshold_ms": (
                    request.threshold_ms
                    if request.threshold_ms is not None
                    else DEFAULT_COMPARISON_THRESHOLD_MS
                ),
                "statistics": {"derived": False},
            }

        # Derived: recompute every baseline original under the recorded cleaning
        # rules; the client's numbers are never trusted.
        all_intervals: list[float] = []
        sample_sizes: list[int] = []
        for target in summaries:
            recomputed = self._recompute_summary(target)
            if not recomputed.available:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    recomputed.reason or "a baseline original cannot be recomputed",
                )
            if not recomputed.intervals:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "a baseline has no valid interval; its threshold cannot be frozen",
                )
            all_intervals.extend(recomputed.intervals)
            sample_sizes.append(len(recomputed.intervals))
        median = statistics.median(all_intervals)
        if not math.isfinite(median) or median <= 0:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the baseline median interval is not usable",
            )
        threshold = max(DEFAULT_COMPARISON_THRESHOLD_MS, 2.0 * median)
        return {
            "threshold_ms": threshold,
            "statistics": {
                "derived": True,
                "baseline_median_ms": median,
                "interval_counts": sample_sizes,
                "total_intervals": len(all_intervals),
            },
        }

    def _validate_baselines(
        self, request: CreateComparisonProtocolRequest, user_scope_id: str
    ) -> list["_SummaryTarget"]:
        if len(set(request.baseline_summary_ids)) != len(request.baseline_summary_ids):
            raise _invalid(
                "baseline_summary_ids", "conflict", "a baseline summary is listed twice"
            )
        refs = [
            (item.segment_id, item.segment_revision, item.binding_revision)
            for item in request.baseline_binding_refs
        ]
        if len(set(refs)) != len(refs):
            raise _invalid(
                "baseline_binding_refs", "conflict", "a baseline binding is listed twice"
            )
        targets = [
            self._load_summary_target(summary_id, user_scope_id)
            for summary_id in request.baseline_summary_ids
        ]
        if request.threshold_mode is ThresholdMode.baseline_derived:
            union = {
                (segment.id, segment.revision, binding.binding_revision)
                for target in targets
                for segment, binding in target.bindings
            }
            if union != set(refs):
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the baseline bindings are not exactly the summaries' own bindings",
                )
        else:
            # A fixed protocol may name baselines; they must still be visible and
            # closed, but they do not have to mirror a summary.
            for ref in request.baseline_binding_refs:
                self.require_analyzeable(ref, user_scope_id)
        artifacts = [target.artifact.id for target in targets]
        if len(set(artifacts)) != len(artifacts):
            raise _invalid(
                "baseline_summary_ids", "conflict",
                "two baselines name the same original interval",
            )
        for index, first in enumerate(targets):
            for second in targets[index + 1:]:
                if (
                    first.session.id == second.session.id
                    and _ranges_overlap(first.summary.analyzed_ranges, second.summary.analyzed_ranges)
                ):
                    raise _invalid(
                        "baseline_summary_ids", "conflict",
                        "two baselines cover overlapping original intervals",
                    )
        if targets:
            games = {target.session.game_id for target in targets}
            if len(games) != 1:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the baselines belong to different games",
                )
            schema = {target.artifact.source_schema for target in targets}
            if len(schema) != 1:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "the baselines use different verified schemas",
                )
        return targets

    def _load_summary_target(self, summary_id: str, user_scope_id: str) -> "_SummaryTarget":
        with self.repository.unit_of_work() as uow:
            stored = uow.find_object("PerformanceSummary", summary_id)
            if stored is None:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested summary is not available"
                )
            summary = PerformanceSummary.model_validate_json(stored.payload_bytes)
            artifact = self._load_artifact(uow, summary.artifact_id)
            session = self._load_session(uow, artifact.session_id)
            self._require_session_owner(uow, session, user_scope_id)
            file_row = uow.find_object("ManagedFile", artifact.file_id)
            file_payload = None if file_row is None else file_row.payload
            if file_payload is not None and file_payload.get("user_scope_id") != user_scope_id:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested summary is not available"
                )
            bindings: list[tuple[SessionSegment, ConfigurationBinding]] = []
            for ref in summary.bindings:
                segment = self._load_segment_revision(
                    uow, ref.segment_id, ref.segment_revision
                )
                binding = self._load_binding(uow, ref.segment_id, ref.binding_revision)
                if segment.lifecycle is not SessionLifecycle.ended or segment.end_s is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the summary names an open segment revision",
                    )
                if binding.effective_range.end is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the summary names an open binding revision",
                    )
                if segment.session_id != session.id or segment.id not in artifact.segment_ids:
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        "the summary's artifact does not cover its binding",
                    )
                bindings.append((segment, binding))
        return _SummaryTarget(
            summary=summary,
            artifact=artifact,
            session=session,
            bindings=bindings,
            file_payload=file_payload,
        )

    def _recompute_summary(self, target: "_SummaryTarget") -> "_Recompute":
        """Re-read the original and rebuild the summary's retained intervals."""
        if target.file_payload is None:
            return _Recompute(False, [], 0.0, None, "the summary's original is not registered")
        if target.file_payload.get("file_status") != "published":
            return _Recompute(False, [], 0.0, None, "the summary's original is not consumable")
        try:
            data = self.files.read_managed_file(target.artifact.file_id)
        except ContractError as error:
            return _Recompute(False, [], 0.0, None, error.message)
        try:
            parsed = parse_log(
                data,
                registry=self.log_registry,
                evidence=_artifact_evidence(target.file_payload),
                deadline_seconds=RECOMPUTE_DEADLINE_SECONDS,
            )
        except ContractError as error:
            return _Recompute(False, [], 0.0, None, error.message)
        if parsed.format_id != target.artifact.source_schema:
            return _Recompute(
                False, [], 0.0, None,
                "the original no longer matches the artifact's verified schema",
            )
        mapping = target.artifact.time_mapping
        if mapping.scale is None or mapping.offset is None:
            return _Recompute(False, [], 0.0, None, "the artifact has no time mapping")
        if len(parsed.observed_streams) > 1:
            return _Recompute(
                False, [], 0.0, None,
                "the original mixes frame streams; it cannot be recomputed as one",
            )
        if parsed.frame_stream_kind is not FrameStreamKind.application_present:
            return _Recompute(
                False, [], 0.0, None,
                "the original's frame stream is not confirmed as application_present",
            )
        intervals: list[float] = []
        for segment, binding in target.bindings:
            window = _analysis_window(segment, binding)
            cleaning = clean_frames(
                parsed.frames,
                time_mapping=mapping,
                window=window,
                excluded_ranges=segment.excluded_ranges,
            )
            intervals.extend(cleaning.intervals)
        return _Recompute(
            True, intervals, sum(intervals) / 1000.0,
            parsed.frame_stream_kind, None,
        )

    def _write_comparison_protocol(
        self,
        uow: UnitOfWork,
        *,
        request: CreateComparisonProtocolRequest,
        protocol_id: str,
        now: str,
        threshold_ms: float,
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        series_id = _deterministic_id(protocol_id, "series")
        protocol = ComparisonProtocol(
            id=protocol_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            series_id=series_id,
            version=1,
            threshold_mode=request.threshold_mode,
            threshold_ms=threshold_ms,
            baseline_summary_ids=list(request.baseline_summary_ids),
            baseline_binding_refs=list(request.baseline_binding_refs),
            frozen_at=now,
            exploratory=request.exploratory,
            cleaning_definition=request.cleaning_definition,
            clustering_definition=request.clustering_definition,
            denominator_definition=request.denominator_definition,
        )
        uow.insert_versioned("ComparisonProtocol", protocol)
        for summary_id in protocol.baseline_summary_ids:
            uow.insert_reference(protocol.id, "baseline_summary", ObjectRef(
                entity_type="PerformanceSummary", entity_id=summary_id,
            ))
        # ``managed[0]`` is not yet an objects row (files.publish inserts it after
        # the writer), so the declaration is referenced by id only.
        return [
            ObjectRef(entity_type="ComparisonProtocol", entity_id=protocol.id),
            ObjectRef(entity_type="ManagedFile", entity_id=managed[0].id),
        ]

    def _load_comparison_protocol_typed(self, protocol_id: str) -> ComparisonProtocol:
        self.require_comparison_protocol(protocol_id)
        with self.repository.unit_of_work() as uow:
            stored = uow.find_object("ComparisonProtocol", protocol_id)
        assert stored is not None
        return ComparisonProtocol.model_validate_json(stored.payload_bytes)

    def compare(
        self,
        request: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
    ) -> ResourceResult:
        route = COMPARISON_ROUTE
        typed = self._compare_input(request)
        request_hash = _request_hash(route, request_schema_version, typed)
        with self.repository.unit_of_work() as uow:
            replay = self._replay_resource(
                uow, context, route, request_schema_version, request_hash
            )
            if replay is not None:
                return replay

        protocol = self._load_comparison_protocol_typed(typed.protocol_id)
        self._require_protocol_owner(protocol.id, context.user_scope_id)
        left = self._load_summary_target(typed.summary_ids[0], context.user_scope_id)
        right = self._load_summary_target(typed.summary_ids[1], context.user_scope_id)
        recomputed_left = self._recompute_summary(left)
        recomputed_right = self._recompute_summary(right)
        self._require_temporal_order(left, right, protocol)

        comparison_id = _deterministic_id(
            context.user_scope_id, route, context.idempotency_key, request_hash, "comparison",
        )
        plan = self._build_comparison(
            protocol, left, right, recomputed_left, recomputed_right, comparison_id,
        )
        now = self.clock()
        with self.repository.unit_of_work() as uow:
            # A concurrent submission of the same key may have committed while
            # this one computed; replay its response instead of writing twice.
            winner = self._replay_resource(
                uow, context, route, request_schema_version, request_hash
            )
            if winner is not None:
                return winner
            # Re-check the frozen protocol and both summaries inside the short
            # publishing transaction; a concurrently removed reference loses.
            if uow.find_object("ComparisonProtocol", protocol.id) is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID, "the comparison protocol disappeared"
                )
            for summary_id in typed.summary_ids:
                if uow.find_object("PerformanceSummary", summary_id) is None:
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID, "a compared summary disappeared"
                    )
            self._register_definitions(uow, plan["metric_rules"], now)
            uow.insert_object("PerformanceComparison", plan["comparison"])
            for summary_id in typed.summary_ids:
                uow.insert_reference(comparison_id, "summary", ObjectRef(
                    entity_type="PerformanceSummary", entity_id=summary_id,
                ))
            uow.insert_reference(comparison_id, "protocol", ObjectRef(
                entity_type="ComparisonProtocol", entity_id=protocol.id,
            ))
            result = ResourceResult(refs=[
                ObjectRef(entity_type="PerformanceComparison", entity_id=comparison_id)
            ])
            self._record_resource(
                uow, context, route, request_schema_version, request_hash, result,
            )
            return result

    def _compare_input(self, request: Any) -> CompareInput:
        if isinstance(request, CompareInput):
            return request
        try:
            return CompareInput.model_validate_json(canonical_json(request))
        except Exception as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the compare input is invalid",
                field_errors=[FieldError(
                    path="input", code="type", message=str(error).splitlines()[0],
                )],
            ) from error

    def _require_temporal_order(
        self,
        left: "_SummaryTarget",
        right: "_SummaryTarget",
        protocol: ComparisonProtocol,
    ) -> None:
        """A non-exploratory protocol must be provably frozen before the later test."""
        if protocol.exploratory:
            return
        try:
            left_start = datetime.fromisoformat(left.session.started_at)
            right_start = datetime.fromisoformat(right.session.started_at)
            frozen = datetime.fromisoformat(protocol.frozen_at)
        except ValueError as error:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the test order has no usable backend time",
            ) from error
        if left_start == right_start:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the two tests have the same start time; their order is unproven",
            )
        later = (left, right)[0] if left_start > right_start else (left, right)[1]
        later_start = max(left_start, right_start)
        if later_start <= frozen:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the later test started before the protocol was frozen",
            )
        later_summary_at = datetime.fromisoformat(later.summary.created_at)
        if later_summary_at <= frozen:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the later test's result was registered before the protocol was frozen",
            )

    def _build_comparison(
        self,
        protocol: ComparisonProtocol,
        left: "_SummaryTarget",
        right: "_SummaryTarget",
        recomputed_left: "_Recompute",
        recomputed_right: "_Recompute",
        comparison_id: str,
    ) -> dict[str, Any]:
        rules = _protocol_metric_rules(protocol)
        left_ref = ObjectRef(entity_type="PerformanceSummary", entity_id=left.summary.id)
        right_ref = ObjectRef(entity_type="PerformanceSummary", entity_id=right.summary.id)
        per_metric: list[MetricComparison] = []
        for left_value in left.summary.metrics:
            right_value = next(
                (item for item in right.summary.metrics
                 if item.definition_id == left_value.definition_id),
                None,
            )
            per_metric.append(_compare_stored_metric(
                left_value, right_value, left_ref, right_ref,
            ))
        per_metric.extend([
            _compare_long_frame_rate(
                rules[0], protocol, left, right, recomputed_left, recomputed_right,
                left_ref, right_ref,
            ),
            _compare_long_frame_share(
                rules[1], protocol, left, right, recomputed_left, recomputed_right,
                left_ref, right_ref,
            ),
        ])

        checks, level, limitations = self._comparability(protocol, left, right)
        comparison = PerformanceComparison(
            id=comparison_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=self.clock(),
            created_by_task_id=None,
            summary_ids=[left.summary.id, right.summary.id],
            protocol_id=protocol.id,
            level=level,
            per_metric=per_metric,
            checks=checks,
            limitations=limitations,
        )
        return {
            "comparison": comparison,
            "metric_rules": list(rules),
        }

    def _comparability(
        self,
        protocol: ComparisonProtocol,
        left: "_SummaryTarget",
        right: "_SummaryTarget",
    ) -> tuple[list[CheckResult], ComparabilityLevel, list[str]]:
        limitations: list[str] = []
        if protocol.exploratory:
            limitations.append(
                "exploratory comparison: it was created after observing the tests and"
                " is not a pre-planned experiment"
            )
        left_segment = left.bindings[0][0] if left.bindings else None
        right_segment = right.bindings[0][0] if right.bindings else None

        def check(check_id: str, status: CheckStatus, note: str) -> CheckResult:
            return _comparison_check(check_id, status, note, self.clock())

        checks: list[CheckResult] = []
        game = CheckStatus.pass_ if left.session.game_id == right.session.game_id else CheckStatus.fail
        checks.append(check(
            "comparison.game", game,
            "both tests name the same game" if game is CheckStatus.pass_
            else "the tests name different games; they are not comparable",
        ))
        device = (
            CheckStatus.pass_
            if left.session.device_snapshot_id == right.session.device_snapshot_id
            else CheckStatus.unknown
        )
        checks.append(check(
            "comparison.device", device,
            "both tests share one device snapshot" if device is CheckStatus.pass_
            else "the device snapshots differ; the same hardware cannot be proven",
        ))
        if left_segment is None or right_segment is None:
            scenario = CheckStatus.unknown
            note = "a summary names no segment revision"
        else:
            scenario = _compare_field(left_segment.scenario, right_segment.scenario)
            note = "the declared scenario is known and equal" if scenario is CheckStatus.pass_ else (
                "the declared scenario differs or is unknown"
            )
        checks.append(check("comparison.scenario", scenario, note))

        if left_segment is None or right_segment is None:
            conditions = CheckStatus.unknown
            conditions_note = "a summary names no segment revision"
        else:
            statuses = _compare_run_conditions(
                left_segment.run_conditions, right_segment.run_conditions
            )
            statuses.append(_compare_stream(
                left_segment.stream_selection, right_segment.stream_selection
            ))
            conditions = _combine_statuses(statuses)
            conditions_note = (
                "resolution, frame generation, upscaling, scene and stream are equal"
                if conditions is CheckStatus.pass_
                else "a run condition differs or is unknown"
            )
        checks.append(check("comparison.frame_conditions", conditions, conditions_note))

        configuration = self._compare_configuration(left_segment, right_segment, limitations)
        checks.append(check(
            "comparison.configuration", configuration,
            "both runs have a bound, confirmed configuration"
            if configuration is CheckStatus.pass_
            else "the effective configuration is unknown, mixed or unconfirmed",
        ))

        protocol_ok = (
            left.summary.analyzer_version == right.summary.analyzer_version
            and left.summary.cleaning_protocol_version == right.summary.cleaning_protocol_version
            and left.summary.cleaning_protocol_version == protocol.cleaning_definition
            and left.summary.analyzer_version == ANALYZER_VERSION
        )
        checks.append(check(
            "comparison.protocol", CheckStatus.pass_ if protocol_ok else CheckStatus.fail,
            "both summaries and the frozen protocol use the same analyzer/cleaning rules"
            if protocol_ok else
            "the analyzer or cleaning definitions differ; the metrics are not one rule set",
        ))

        critical = {"comparison.game", "comparison.scenario",
                    "comparison.frame_conditions", "comparison.configuration",
                    "comparison.protocol"}
        by_id = {item.check_id: item for item in checks}
        if any(by_id[item].status is CheckStatus.fail for item in critical):
            level = ComparabilityLevel.descriptive
        elif any(by_id[item].status is CheckStatus.unknown for item in critical):
            level = ComparabilityLevel.descriptive
        elif by_id["comparison.device"].status is not CheckStatus.pass_:
            level = ComparabilityLevel.approximate
            limitations.append(
                "the device snapshot is not identical; the difference is a minor"
                " unexplained condition"
            )
        else:
            level = ComparabilityLevel.controlled
        return checks, level, limitations

    def _compare_configuration(
        self,
        left: SessionSegment | None,
        right: SessionSegment | None,
        limitations: list[str],
    ) -> CheckStatus:
        if left is None or right is None:
            return CheckStatus.unknown
        if (
            left.configuration_state is not ConfigurationState.bound
            or right.configuration_state is not ConfigurationState.bound
            or left.applied_id is None
            or right.applied_id is None
        ):
            return CheckStatus.unknown
        if left.applied_id == right.applied_id:
            return CheckStatus.pass_
        activations: list[Any] = []
        with self.repository.unit_of_work() as uow:
            for applied_id in (left.applied_id, right.applied_id):
                stored = uow.find_object("AppliedConfiguration", applied_id)
                activations.append(None if stored is None else stored.payload)
        if any(
            item is None or item.get("activation_state") != "active_confirmed"
            for item in activations
        ):
            return CheckStatus.unknown
        limitations.append(
            "the two runs used different adopted configurations; only declared"
            " settings are claimed to differ"
        )
        return CheckStatus.pass_

    def get_comparison(self, comparison_id: str, user_scope_id: str) -> PerformanceComparison:
        with self.repository.unit_of_work() as uow:
            stored = uow.find_object("PerformanceComparison", comparison_id)
            if stored is None:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested comparison is not available"
                )
            comparison = PerformanceComparison.model_validate_json(stored.payload_bytes)
        # Resolve ownership outside the read transaction: every summary load
        # opens its own short transaction.
        for summary_id in comparison.summary_ids:
            self._load_summary_target(summary_id, user_scope_id)
        return comparison

    def get_comparison_protocol(
        self, protocol_id: str, user_scope_id: str
    ) -> ComparisonProtocol:
        protocol = self._load_comparison_protocol_typed(protocol_id)
        self._require_protocol_owner(protocol.id, user_scope_id)
        for summary_id in protocol.baseline_summary_ids:
            self._load_summary_target(summary_id, user_scope_id)
        return protocol

    def _require_protocol_owner(self, protocol_id: str, user_scope_id: str) -> None:
        """The frozen protocol belongs to the user whose submission wrote it."""
        with self.repository.unit_of_work() as uow:
            rows = uow.query(
                "SELECT user_scope_id FROM file_operations WHERE owner_ref=?",
                (protocol_id,),
            )
        if not rows:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the protocol records no owning submission",
            )
        if rows[0]["user_scope_id"] != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested protocol is not available"
            )

    # -- owned capture, leases and safe stop (04 §4.3/§5) -------------------
    def capture(self, request: Any, context: MutationContext) -> TaskAccepted:
        typed = self._capture_input(request)
        # A registered key replays its original TaskAccepted; the launch guards
        # (adapter availability, an active capture) must not run a second time
        # for it.  Different content still conflicts inside register_task.
        registered = self._has_registered(context, CAPTURE_ROUTE)
        if not registered:
            self._capture_adapter(typed.adapter_id, typed.adapter_version)
            with self.repository.unit_of_work() as uow:
                self._capture_guard(uow, typed, context.user_scope_id)
        if self.tasks is None:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR, "the capture handler is not wired"
            )
        return self.tasks.register_task(
            typed, context,
            companion=self._lease_companion(typed, context.user_scope_id),
        )

    def _capture_input(self, request: Any) -> CaptureInput:
        if isinstance(request, CaptureInput):
            return request
        try:
            return CaptureInput.model_validate_json(canonical_json(request))
        except Exception as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the capture input is invalid",
                field_errors=[FieldError(
                    path="input", code="type", message=str(error).splitlines()[0],
                )],
            ) from error

    def _capture_adapter(self, adapter_id: str, adapter_version: str) -> Any:
        registry = self.capture_registry
        adapter = None if registry is None else registry.get(adapter_id, adapter_version)
        if adapter is None:
            raise _invalid(
                "adapter_id", "enum",
                "only a registered and verified capture adapter may run",
            )
        return adapter

    def _capture_guard(
        self, uow: UnitOfWork, request: CaptureInput, user_scope_id: str,
        *, exclude_task_id: str | None = None,
    ) -> tuple[PlaySession, SessionSegment]:
        session = self._load_session(uow, request.session_id)
        self._require_session_owner(uow, session, user_scope_id)
        if session.lifecycle is not SessionLifecycle.active:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "a capture needs an active session",
            )
        segment = self._load_segment_revision(
            uow, request.segment_ref.segment_id, request.segment_ref.segment_revision
        )
        if segment.session_id != session.id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the capture segment belongs to another session",
            )
        head = self._load_segment(uow, segment.id)
        if (
            head.revision != request.segment_ref.segment_revision
            or head.binding_revision != request.segment_ref.binding_revision
        ):
            raise ContractError(
                ErrorCode.BINDING_REVISION_CONFLICT,
                "the segment or its binding changed; re-read it before capturing",
                details=empty_details(
                    expected_revision=request.segment_ref.segment_revision,
                    current_revision=head.revision,
                    current_resource_ref=ObjectRef(
                        entity_type="SessionSegment", entity_id=head.id,
                    ),
                ),
            )
        if segment.lifecycle is not SessionLifecycle.active:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "a capture needs an active segment",
            )
        binding = self._load_binding(uow, segment.id, request.segment_ref.binding_revision)
        if binding.effective_range.end is not None:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "a capture needs an open binding revision",
            )
        if request.protocol_id is not None:
            self._test_protocol_ref(uow, request.protocol_id, session.game_id)

        # The lease is the primary guard; a capture task that has not yet
        # finished closing also keeps the single slot.
        active_tasks = uow.query(
            "SELECT t.task_id, i.input FROM tasks t JOIN task_inputs i"
            " ON t.task_id=i.task_id AND i.input_revision=t.current_input_revision"
            " WHERE t.type='capture' AND t.state IN ('queued','running')"
        )
        for active in active_tasks:
            if active["task_id"] == exclude_task_id:
                continue
            payload = json.loads(active["input"])
            if payload.get("session_id") == session.id:
                raise ContractError(
                    ErrorCode.ACTIVE_CAPTURE_EXISTS,
                    "this session already has a queued or running capture",
                    resource_refs=[ObjectRef(
                        entity_type="TaskRun", entity_id=active["task_id"],
                    )],
                )
        lease = uow.query(
            "SELECT * FROM capture_leases WHERE session_id=? AND state <> 'finalized'"
            " ORDER BY created_at LIMIT 1",
            (session.id,),
        )
        if lease:
            task_state = self._task_state(uow, lease[0]["task_id"])
            reference = ObjectRef(
                entity_type="TaskRun", entity_id=lease[0]["task_id"],
            )
            if task_state in (TaskState.queued, TaskState.running):
                raise ContractError(
                    ErrorCode.ACTIVE_CAPTURE_EXISTS,
                    "this session already has a queued or running capture",
                    resource_refs=[reference],
                )
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "an unfinished capture lease blocks a second capture",
                resource_refs=[reference],
                field_errors=[FieldError(
                    path="session_id", code="conflict",
                    message=f"lease state is {lease[0]['state']}",
                )],
            )
        return session, segment

    def _lease_companion(self, request: CaptureInput, user_scope_id: str):
        def companion(uow: UnitOfWork, task_id: str) -> None:
            session, _ = self._capture_guard(
                uow, request, user_scope_id, exclude_task_id=task_id,
            )
            now = self.clock()
            native_name = f"game-app-capture-{task_id}"
            file_id = self.files.artifact_file_id(f"capture:{task_id}")
            lease = CaptureLease(
                id=task_id,
                schema_version=PRODUCT_CONTRACT_VERSION,
                created_at=now,
                created_by_task_id=task_id,
                task_id=task_id,
                session_id=session.id,
                native_name=native_name,
                process_pid=None,
                process_started_at=None,
                file_id=file_id,
                state="prepared",
            )
            uow.insert_object("CaptureLease", lease)
            try:
                uow.execute(
                    "INSERT INTO capture_leases(task_id, session_id, native_name,"
                    " process_pid, process_started_at, file_id, state, created_at, updated_at)"
                    " VALUES(?, ?, ?, NULL, NULL, ?, 'prepared', ?, ?)",
                    (task_id, session.id, native_name, file_id, now, now),
                )
            except sqlite3.IntegrityError as error:
                concurrent = uow.query(
                    "SELECT task_id FROM capture_leases WHERE session_id=?"
                    " AND state <> 'finalized' LIMIT 1",
                    (session.id,),
                )
                reference = ObjectRef(
                    entity_type="TaskRun",
                    entity_id=concurrent[0]["task_id"] if concurrent else task_id,
                )
                raise ContractError(
                    ErrorCode.ACTIVE_CAPTURE_EXISTS,
                    "this session already has an unfinished capture lease",
                    resource_refs=[reference],
                ) from error
            self._append_lease_event(
                uow, task_id, None, "prepared", "capture_prepared",
                now=now, file_id=file_id,
            )

        return companion

    def handle_capture(self, context: TaskContext) -> HandlerOutcome:
        task_input = context.task_input
        if not isinstance(task_input, CaptureInput):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the capture handler received another task type",
            )
        adapter = self._capture_adapter(
            task_input.adapter_id, task_input.adapter_version
        )
        with self.repository.unit_of_work() as uow:
            lease = self._lease_row(uow, context.task_id)
        if lease is None:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR, "the capture task has no lease"
            )
        state = lease["state"]
        if state == "finalized":
            return self._capture_existing_result(context.task_id)
        if state == "unresolved":
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the capture lease is unresolved; ownership cannot be proven",
            )
        if state == "prepared":
            context.phase("capture")
            with self.repository.unit_of_work() as uow:
                self._transition_lease(
                    uow, context.task_id, "prepared", "owned", "capture_owned",
                )
            try:
                handle = adapter.start(native_name=lease["native_name"])
            except Exception as error:  # noqa: BLE001 - launch outcome is unknown
                self._mark_lease_unresolved(
                    context.task_id, "capture_launch_failed"
                )
                raise ContractError(
                    ErrorCode.INTERNAL_ERROR,
                    "the capture tool could not be started; the lease is unresolved",
                ) from error
            with self.repository.unit_of_work() as uow:
                self._record_lease_ownership(uow, context.task_id, handle)
        else:
            handle = self._capture_handle(lease)

        if not (
            self._stop_requested(context.task_id)
            or self._cancel_requested(context.task_id)
        ):
            # The task keeps the single running slot; stop_capture appends the
            # intent and the next run_once call re-enters this handler.
            return HandlerOutcome(state=TaskState.running)
        context.phase("finalize")
        return self._finalize_capture(context, task_input, adapter, handle)

    def _capture_handle(self, lease: Mapping[str, Any]) -> CaptureHandle:
        if (
            lease["process_pid"] is None
            or lease["process_started_at"] is None
        ):
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the owned lease records no process; a second launch is refused",
            )
        return CaptureHandle(
            native_name=lease["native_name"],
            pid=lease["process_pid"],
            started_at=lease["process_started_at"],
        )

    def _finalize_capture(
        self,
        context: TaskContext,
        task_input: CaptureInput,
        adapter: Any,
        handle: CaptureHandle,
    ) -> HandlerOutcome:
        task_id = context.task_id
        user_scope_id = context.user_scope_id
        if not adapter.verify_ownership(
            native_name=handle.native_name,
            pid=handle.pid,
            started_at=handle.started_at,
        ):
            self._mark_lease_unresolved(task_id, "capture_ownership_unverified")
            return HandlerOutcome(
                state=TaskState.failed,
                stop_reason=TaskStopReason(kind="execution", code="validation_failed"),
                error_code=ErrorCode.PRECONDITION_UNVERIFIED.value,
            )

        output = adapter.stop(handle)
        if output.data is None:
            self._mark_lease_unresolved(task_id, "capture_output_not_ready")
            return HandlerOutcome(
                state=TaskState.partial,
                domain_result=self._capture_domain_result(
                    context, "retained_only",
                    "the tool produced no output; the segment keeps its open end",
                ),
            )

        relative_name = f"captures/{task_id}.csv"
        try:
            file_id = self.files.store_artifact(
                output.data,
                relative_name=relative_name,
                stable_key=f"capture:{task_id}",
                user_scope_id=user_scope_id,
            )
        except ContractError:
            self._mark_lease_unresolved(task_id, "capture_output_unpublished")
            raise
        with self.repository.unit_of_work() as uow:
            lease = self._lease_row(uow, task_id)
        if lease is None or lease["file_id"] != file_id:
            self._mark_lease_unresolved(task_id, "capture_file_identity_changed")
            raise ContractError(
                ErrorCode.INTERNAL_ERROR,
                "the capture output does not match the lease's planned file",
            )

        content_hash = hashlib.sha256(output.data).hexdigest()
        evidence = EvidenceRef(
            kind="artifact", file_id=file_id,
            content_hash=content_hash, locator=relative_name,
        )
        try:
            parsed = parse_log(
                output.data, registry=self.log_registry, evidence=evidence,
            )
        except ContractError as error:
            with self.repository.unit_of_work() as uow:
                self._transition_lease(
                    uow, task_id, "owned", "finalized", "capture_retained_only",
                )
            return HandlerOutcome(
                state=TaskState.partial,
                domain_result=self._capture_domain_result(
                    context, "retained_only",
                    f"the output is not a verified format ({error.message});"
                    " the original is retained and the segment is not closed",
                ),
                result_refs=[ObjectRef(
                    entity_type="ManagedFile", entity_id=file_id,
                )],
            )

        with self.repository.unit_of_work() as uow:
            session, segment = self._capture_guard_for_stop(
                uow, task_input, task_id, user_scope_id
            )
        now = self.clock()
        try:
            end_s: float | None = self._elapsed_seconds(session, segment)
        except ContractError:
            end_s = None
        offset = _capture_offset(session, handle)
        mapping = _capture_time_mapping(parsed, offset, now)
        stream = _capture_stream(parsed, now)
        artifact_id = _deterministic_id(task_id, "capture-artifact")
        closed: ConfigurationBinding | None = None
        updated_segment: SessionSegment | None = None
        with self.repository.unit_of_work() as uow:
            head = self._load_segment(uow, segment.id)
            can_close = (
                end_s is not None
                and head.lifecycle is SessionLifecycle.active
                and head.revision == task_input.segment_ref.segment_revision
                and head.binding_revision == task_input.segment_ref.binding_revision
            )
            if can_close:
                closed = self._close_segment(uow, segment, session, end_s, now=now)
                updated_segment = segment.model_copy(update={
                    "revision": segment.revision + 1,
                    "end_s": end_s,
                    "lifecycle": SessionLifecycle.ended,
                    "binding_revision": closed.binding_revision,
                    "stream_selection": stream,
                })
                self._write_segment_revision(
                    uow, segment, updated_segment, session, cause="capture_stopped",
                )
            artifact = CaptureArtifact(
                id=artifact_id,
                schema_version=PRODUCT_CONTRACT_VERSION,
                created_at=now,
                created_by_task_id=task_id,
                session_id=session.id,
                segment_ids=[segment.id],
                file_id=file_id,
                tool_name=adapter.adapter_id,
                tool_version=adapter.adapter_version,
                options={"native_name": handle.native_name},
                source_schema=parsed.source_schema,
                source_column_map=list(parsed.column_map),
                time_mapping=mapping,
                quality=parsed.quality,
                checks=list(parsed.checks) + [
                    _capture_check(
                        "capture.ownership", CheckStatus.pass_,
                        "the stopped process matched the lease's native name, pid and"
                        " start time", now,
                    ),
                    _capture_check(
                        "capture.stop", CheckStatus.pass_,
                        "only this task's verified process was stopped", now,
                    ),
                    _capture_check(
                        "capture.time_origin",
                        CheckStatus.pass_ if mapping.offset is not None
                        else CheckStatus.unknown,
                        "the output's zero is the backend-measured tool start"
                        if mapping.offset is not None else
                        "the output's clock cannot be mapped to the session", now,
                    ),
                ],
            )
            uow.insert_object("CaptureArtifact", artifact)
            uow.insert_reference(artifact.id, "session", ObjectRef(
                entity_type="PlaySession", entity_id=session.id,
            ))
            uow.insert_reference(artifact.id, "segment", ObjectRef(
                entity_type="SessionSegment", entity_id=segment.id,
            ))
            uow.insert_reference(artifact.id, "file", ObjectRef(
                entity_type="ManagedFile", entity_id=file_id,
            ))
            self._transition_lease(
                uow, task_id, "owned", "finalized",
                "capture_finalized" if can_close else "capture_retained_only",
            )

        refs = [
            ObjectRef(entity_type="CaptureArtifact", entity_id=artifact_id),
            ObjectRef(entity_type="ManagedFile", entity_id=file_id),
        ]
        if can_close and closed is not None and updated_segment is not None:
            refs.extend([
                ObjectRef(entity_type="SessionSegment", entity_id=updated_segment.id),
                ObjectRef(
                    entity_type="ConfigurationBinding", entity_id=closed.id,
                ),
            ])
        return HandlerOutcome(
            state=TaskState.completed if can_close else TaskState.partial,
            domain_result=self._capture_domain_result(
                context, "captured" if can_close else "retained_only",
                "the original is published and the segment is closed"
                if can_close else
                "the original is published; the segment keeps its open end",
            ),
            result_refs=refs,
        )

    def _capture_guard_for_stop(
        self, uow: UnitOfWork, request: CaptureInput, task_id: str, user_scope_id: str
    ) -> tuple[PlaySession, SessionSegment]:
        session = self._load_session(uow, request.session_id)
        self._require_session_owner(uow, session, user_scope_id)
        segment = self._load_segment_revision(
            uow, request.segment_ref.segment_id, request.segment_ref.segment_revision
        )
        if segment.session_id != session.id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the capture segment belongs to another session",
            )
        return session, segment

    def _capture_domain_result(
        self, context: TaskContext, code: str, note: str
    ) -> DomainResult:
        return DomainResult(
            domain=TaskType.capture,
            code=code,
            input_revision=context.task_input_revision,
            checks=[_capture_check(
                "capture.result",
                CheckStatus.pass_ if code == "captured" else CheckStatus.unknown,
                note, self.clock(),
            )],
        )

    def _capture_existing_result(self, task_id: str) -> HandlerOutcome:
        view = self.tasks.get_task(task_id)
        return HandlerOutcome(
            state=TaskState.completed, result_refs=list(view.task.result_refs),
        )

    def stop_capture(
        self,
        request: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
    ) -> ResourceResult:
        typed = self._stop_capture_input(request)
        route = CAPTURE_STOP_ROUTE.format(task_id=typed.task_id)
        request_hash = _request_hash(route, request_schema_version, typed)
        with self.repository.unit_of_work() as uow:
            replay = self._replay_resource(
                uow, context, route, request_schema_version, request_hash
            )
            if replay is not None:
                return replay
            row = self.tasks._task_row(uow, typed.task_id)
            if row["user_scope_id"] != context.user_scope_id:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested task is not available"
                )
            if row["type"] != TaskType.capture.value:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID, "the task is not a capture"
                )
            state = TaskState(row["state"])
            reference = ObjectRef(entity_type="TaskRun", entity_id=typed.task_id)
            if state in TERMINAL_TASK_STATES:
                result = ResourceResult(
                    refs=list(
                        [ObjectRef.model_validate(item) for item in
                         json.loads(row["result_refs"] or "[]")]
                    ) or [reference]
                )
                self._record_resource(
                    uow, context, route, request_schema_version, request_hash, result,
                )
                return result
            now = self.clock()
            revision = row["revision"] + 1
            if state in (TaskState.queued, TaskState.waiting_input):
                stop = TaskStopReason(kind="execution", code="user_cancelled")
                uow.execute(
                    "UPDATE tasks SET state='cancelled', cancel_requested=1,"
                    " stop_reason=?, revision=?, updated_at=? WHERE task_id=?",
                    (stop.model_dump_json(), revision, now, typed.task_id),
                )
                self.tasks._append_event(
                    uow, typed.task_id, self.tasks._next_sequence(uow, typed.task_id),
                    row["current_input_revision"], state, TaskState.cancelled,
                    "capture_stop_cancelled",
                    active_elapsed=row["active_elapsed_seconds"],
                )
                self._finalize_unstarted_lease(uow, typed.task_id)
                result = ResourceResult(refs=[reference])
            else:
                uow.execute(
                    "UPDATE tasks SET revision=?, updated_at=? WHERE task_id=?",
                    (revision, now, typed.task_id),
                )
                if not self._stop_event(uow, typed.task_id):
                    self.tasks._append_event(
                        uow, typed.task_id,
                        self.tasks._next_sequence(uow, typed.task_id),
                        row["current_input_revision"], TaskState.running, TaskState.running,
                        "capture_stop_requested",
                        active_elapsed=row["active_elapsed_seconds"],
                    )
                result = ResourceResult(refs=[reference])
            self._record_resource(
                uow, context, route, request_schema_version, request_hash, result,
            )
            return result

    def _stop_capture_input(self, request: Any) -> StopCaptureInput:
        if isinstance(request, StopCaptureInput):
            return request
        try:
            return StopCaptureInput.model_validate_json(canonical_json(request))
        except Exception as error:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the stop-capture input is invalid",
                field_errors=[FieldError(
                    path="input", code="type", message=str(error).splitlines()[0],
                )],
            ) from error

    def _finalize_unstarted_lease(self, uow: UnitOfWork, task_id: str) -> None:
        lease = self._lease_row(uow, task_id)
        if lease is None or lease["state"] not in ("prepared", "owned"):
            return
        self._transition_lease(
            uow, task_id, lease["state"], "finalized",
            "capture_cancelled_before_start",
        )

    def get_capture_lease(self, task_id: str, user_scope_id: str) -> CaptureLease:
        with self.repository.unit_of_work() as uow:
            row = self.tasks._task_row(uow, task_id)
            if row["user_scope_id"] != user_scope_id:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested task is not available"
                )
            lease = self._lease_row(uow, task_id)
        if lease is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested capture lease is not available"
            )
        return CaptureLease(
            id=lease["task_id"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=lease["created_at"],
            created_by_task_id=lease["task_id"],
            task_id=lease["task_id"],
            session_id=lease["session_id"],
            native_name=lease["native_name"],
            process_pid=lease["process_pid"],
            process_started_at=lease["process_started_at"],
            file_id=lease["file_id"],
            state=lease["state"],
        )

    def reconcile_capture_leases(self) -> list[str]:
        """After a restart, only stop a process this task can prove it owns."""
        unresolved: list[str] = []
        with self.repository.unit_of_work() as uow:
            leases = uow.query(
                "SELECT * FROM capture_leases WHERE state IN ('prepared','owned')"
            )
        for lease in leases:
            task_id = lease["task_id"]
            with self.repository.unit_of_work() as uow:
                state = self._task_state(uow, task_id)
            if state not in TERMINAL_TASK_STATES:
                continue
            if lease["state"] == "prepared":
                with self.repository.unit_of_work() as uow:
                    self._transition_lease(
                        uow, task_id, "prepared", "finalized", "capture_never_started",
                    )
                continue
            try:
                handle = self._capture_handle(lease)
                task_input = self.tasks._load_input(task_id)
                adapter = self._capture_adapter(
                    task_input.adapter_id, task_input.adapter_version
                )
                owned = adapter.verify_ownership(
                    native_name=handle.native_name,
                    pid=handle.pid,
                    started_at=handle.started_at,
                )
            except (ContractError, AttributeError):
                owned = False
                adapter = None
                handle = None
            if not owned or adapter is None or handle is None:
                self._mark_lease_unresolved(task_id, "capture_ownership_unverified")
                unresolved.append(task_id)
                continue
            output = adapter.stop(handle)
            if output.data is None:
                self._mark_lease_unresolved(task_id, "capture_output_not_ready")
                unresolved.append(task_id)
                continue
            try:
                self.files.store_artifact(
                    output.data,
                    relative_name=f"captures/{task_id}.csv",
                    stable_key=f"capture:{task_id}",
                    user_scope_id=self._task_owner(task_id),
                )
            except ContractError:
                self._mark_lease_unresolved(task_id, "capture_output_unpublished")
                unresolved.append(task_id)
                continue
            with self.repository.unit_of_work() as uow:
                self._transition_lease(
                    uow, task_id, "owned", "finalized", "capture_recovered",
                )
        return unresolved

    def _task_owner(self, task_id: str) -> str:
        with self.repository.unit_of_work() as uow:
            return str(self.tasks._task_row(uow, task_id)["user_scope_id"])

    def _task_state(self, uow: UnitOfWork, task_id: str) -> TaskState | None:
        rows = uow.query("SELECT state FROM tasks WHERE task_id=?", (task_id,))
        return None if not rows else TaskState(rows[0]["state"])

    def _lease_row(self, uow: UnitOfWork, task_id: str) -> Any:
        rows = uow.query("SELECT * FROM capture_leases WHERE task_id=?", (task_id,))
        return None if not rows else rows[0]

    def _append_lease_event(
        self,
        uow: UnitOfWork,
        task_id: str,
        previous_state: str | None,
        next_state: str,
        cause: str,
        *,
        now: str,
        process_pid: int | None = None,
        process_started_at: str | None = None,
        file_id: str | None = None,
    ) -> None:
        sequence = uow.query(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS n FROM capture_lease_events"
            " WHERE task_id=?",
            (task_id,),
        )[0]["n"]
        uow.execute(
            "INSERT INTO capture_lease_events(task_id, sequence, previous_state,"
            " next_state, cause, process_pid, process_started_at, file_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, sequence, previous_state, next_state, cause,
                process_pid, process_started_at, file_id, now,
            ),
        )

    def _transition_lease(
        self,
        uow: UnitOfWork,
        task_id: str,
        expected: str,
        next_state: str,
        cause: str,
        *,
        process_pid: int | None = None,
        process_started_at: str | None = None,
    ) -> Any:
        lease = self._lease_row(uow, task_id)
        if lease is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the capture lease is not available"
            )
        if lease["state"] != expected:
            raise ContractError(
                ErrorCode.TASK_STATE_CONFLICT,
                f"the capture lease is {lease['state']}, not {expected}",
            )
        now = self.clock()
        pid = process_pid if process_pid is not None else lease["process_pid"]
        started = (
            process_started_at if process_started_at is not None
            else lease["process_started_at"]
        )
        cursor = uow.execute(
            "UPDATE capture_leases SET state=?, process_pid=?, process_started_at=?,"
            " updated_at=? WHERE task_id=? AND state=?",
            (next_state, pid, started, now, task_id, expected),
        )
        if cursor.rowcount != 1:
            raise ContractError(
                ErrorCode.TASK_STATE_CONFLICT,
                "the capture lease changed while it was being updated",
            )
        self._append_lease_event(
            uow, task_id, expected, next_state, cause,
            now=now, process_pid=pid, process_started_at=started,
            file_id=lease["file_id"],
        )
        return lease

    def _record_lease_ownership(
        self, uow: UnitOfWork, task_id: str, handle: CaptureHandle
    ) -> None:
        lease = self._lease_row(uow, task_id)
        if lease is None or lease["state"] != "owned":
            raise ContractError(
                ErrorCode.TASK_STATE_CONFLICT,
                "ownership facts can only be recorded on an owned lease",
            )
        now = self.clock()
        uow.execute(
            "UPDATE capture_leases SET process_pid=?, process_started_at=?, updated_at=?"
            " WHERE task_id=? AND state='owned'",
            (handle.pid, handle.started_at, now, task_id),
        )
        self._append_lease_event(
            uow, task_id, "owned", "owned", "capture_started",
            now=now, process_pid=handle.pid,
            process_started_at=handle.started_at, file_id=lease["file_id"],
        )

    def _mark_lease_unresolved(self, task_id: str, cause: str) -> None:
        with self.repository.unit_of_work() as uow:
            lease = self._lease_row(uow, task_id)
            if lease is None or lease["state"] not in ("prepared", "owned"):
                return
            self._transition_lease(
                uow, task_id, lease["state"], "unresolved", cause,
            )

    def _stop_event(self, uow: UnitOfWork, task_id: str) -> bool:
        return bool(uow.query(
            "SELECT 1 FROM task_events WHERE task_id=?"
            " AND cause='capture_stop_requested' LIMIT 1",
            (task_id,),
        ))

    def _stop_requested(self, task_id: str) -> bool:
        with self.repository.unit_of_work() as uow:
            return self._stop_event(uow, task_id)

    def _cancel_requested(self, task_id: str) -> bool:
        with self.repository.unit_of_work() as uow:
            row = self.tasks._task_row(uow, task_id)
            return bool(row["cancel_requested"])

    # -- segment internals --------------------------------------------------
    def _segment_and_session(
        self, uow: UnitOfWork, segment_id: str, user_scope_id: str
    ) -> tuple[SessionSegment, PlaySession]:
        segment = self._load_segment(uow, segment_id)
        session = self._load_session(uow, segment.session_id)
        self._require_session_owner(uow, session, user_scope_id)
        return segment, session

    def _load_segment_revision(
        self, uow: UnitOfWork, segment_id: str, revision: int
    ) -> SessionSegment:
        if uow.get_head("SessionSegment", segment_id) is None:
            stored = uow.find_object("SessionSegment", segment_id)
            if stored is None:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested segment is not available"
                )
            return SessionSegment.model_validate_json(stored.payload_bytes)
        try:
            payload = uow.get_snapshot("SessionSegment", segment_id, revision)
        except ContractError as error:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the requested segment revision does not exist",
                field_errors=[FieldError(
                    path="segment_revision", code="reference",
                    message="no such stored revision",
                )],
            ) from error
        return SessionSegment.model_validate_json(canonical_json(payload))

    def _load_segment(self, uow: UnitOfWork, segment_id: str) -> SessionSegment:
        head = uow.execute(
            "SELECT * FROM session_segments WHERE segment_id=?", (segment_id,)
        ).fetchone()
        if head is None:
            stored = uow.find_object("SessionSegment", segment_id)
            if stored is None:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested segment is not available"
                )
            return SessionSegment.model_validate_json(stored.payload_bytes)
        revision = uow.get_head("SessionSegment", segment_id)
        if revision is None:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR, "the segment projection has no stored revision"
            )
        return SessionSegment.model_validate_json(
            canonical_json(uow.get_snapshot("SessionSegment", segment_id, revision))
        )

    def _load_binding(
        self, uow: UnitOfWork, segment_id: str, binding_revision: int
    ) -> ConfigurationBinding:
        row = uow.execute(
            "SELECT * FROM configuration_bindings WHERE segment_id=? AND binding_revision=?",
            (segment_id, binding_revision),
        ).fetchone()
        if row is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested binding revision is not available"
            )
        stored = uow.find_object("ConfigurationBinding", row["binding_id"])
        if stored is None:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR, "the binding projection points at no stored record"
            )
        return ConfigurationBinding.model_validate_json(stored.payload_bytes)

    def _active_segments(self, uow: UnitOfWork, session_id: str) -> list[SessionSegment]:
        rows = uow.execute(
            "SELECT segment_id FROM session_segments"
            " WHERE session_id=? AND lifecycle='active' ORDER BY start_s, segment_id",
            (session_id,),
        ).fetchall()
        return [self._load_segment(uow, row["segment_id"]) for row in rows]

    def _require_exact_segment_set(
        self, active: Sequence[SessionSegment], submitted: Sequence[EndSegmentRequest]
    ) -> None:
        active_ids = {segment.id for segment in active}
        submitted_ids = [item.segment_id for item in submitted]
        if len(set(submitted_ids)) != len(submitted_ids):
            raise _invalid("active_segments", "conflict", "a segment is listed twice")
        if set(submitted_ids) != active_ids:
            missing = sorted(active_ids - set(submitted_ids))
            extra = sorted(set(submitted_ids) - active_ids)
            raise _invalid(
                "active_segments", "conflict",
                f"the active segment set differs: missing {missing}, unexpected {extra}",
            )

    def _require_current_revisions(
        self,
        segment: SessionSegment,
        *,
        expected_segment_revision: int,
        expected_binding_revision: int,
    ) -> None:
        errors: list[FieldError] = []
        if segment.revision != expected_segment_revision:
            errors.append(FieldError(
                path="segment_revision", code="conflict",
                message=f"current revision is {segment.revision}",
            ))
        if segment.binding_revision != expected_binding_revision:
            errors.append(FieldError(
                path="binding_revision", code="conflict",
                message=f"current binding revision is {segment.binding_revision}",
            ))
        if errors:
            raise ContractError(
                ErrorCode.BINDING_REVISION_CONFLICT,
                "the segment or its binding changed; re-read it before submitting",
                field_errors=errors,
                details=empty_details(
                    expected_revision=expected_segment_revision,
                    current_revision=segment.revision,
                    current_resource_ref=ObjectRef(
                        entity_type="SessionSegment", entity_id=segment.id,
                    ),
                ),
            )

    def _require_active_segment(
        self, segment: SessionSegment, request: EndSegmentRequest
    ) -> None:
        self._require_current_revisions(
            segment,
            expected_segment_revision=request.expected_segment_revision,
            expected_binding_revision=request.expected_binding_revision,
        )
        if segment.lifecycle is not SessionLifecycle.active:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED, "the segment is already ended"
            )

    def _close_segment(
        self,
        uow: UnitOfWork,
        segment: SessionSegment,
        session: PlaySession,
        end_s: float,
        *,
        now: str,
    ) -> ConfigurationBinding:
        return self._close_segment_at(
            uow, segment, session, end_s, now=now, evidence_refs=[],
        )

    def _close_segment_at(
        self,
        uow: UnitOfWork,
        segment: SessionSegment,
        session: PlaySession,
        end_s: float | None,
        *,
        now: str,
        evidence_refs: Sequence[EvidenceRef],
    ) -> ConfigurationBinding:
        if end_s is None or not math.isfinite(end_s) or end_s <= segment.start_s:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the backend clock did not provide a reliable end point; the segment stays open",
            )
        row = uow.execute(
            "SELECT * FROM configuration_bindings WHERE segment_id=? AND binding_revision=?",
            (segment.id, segment.binding_revision),
        ).fetchone()
        if row is None:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR, "the current binding projection is missing"
            )
        revision = segment.binding_revision + 1
        binding = ConfigurationBinding(
            id=_deterministic_id(
                session.id, segment.id, "close", str(revision),
            ),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            segment_id=segment.id,
            binding_revision=revision,
            applied_id=segment.applied_id,
            configuration_state=segment.configuration_state,
            effective_range=BindingRange(
                start=row["start_s"], end=end_s,
                time_basis=row["time_basis"], unit=row["unit"],
            ),
            evidence_refs=list(evidence_refs),
            supersedes_revision=segment.binding_revision,
        )
        uow.insert_object("ConfigurationBinding", binding)
        self._insert_binding_row(uow, binding, now)
        return binding

    def _write_segment_revision(
        self,
        uow: UnitOfWork,
        previous: SessionSegment,
        updated: SessionSegment,
        session: PlaySession,
        *,
        cause: str,
    ) -> None:
        head = uow.get_head("SessionSegment", previous.id)
        if head != previous.revision:
            raise ContractError(
                ErrorCode.INTERNAL_ERROR, "the segment projection is not at the revision read"
            )
        uow.append_snapshot("SessionSegment", updated.id, updated)
        cursor = uow.execute(
            "UPDATE session_segments SET revision=?, start_s=?, end_s=?, time_basis=?,"
            " lifecycle=?, applied_id=?, configuration_state=?, binding_revision=?, updated_at=?"
            " WHERE session_id=? AND segment_id=? AND revision=?",
            (
                updated.revision, updated.start_s, updated.end_s, updated.time_basis,
                updated.lifecycle.value, updated.applied_id,
                updated.configuration_state.value, updated.binding_revision,
                self.clock(), updated.session_id, updated.id, previous.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise ContractError(
                ErrorCode.BINDING_REVISION_CONFLICT,
                "the segment changed while it was being updated",
            )
        uow.record_domain_event(
            target_type="SessionSegment", target_id=updated.id, dimension="session",
            sequence=updated.revision, previous_state=previous.lifecycle.value,
            next_state=updated.lifecycle.value, cause=cause,
            related_refs=[ObjectRef(entity_type="PlaySession", entity_id=session.id)],
            created_at=self.clock(),
        )

    def _insert_segment_head(
        self, uow: UnitOfWork, segment: SessionSegment, segment_id: str, now: str
    ) -> None:
        uow.execute(
            "INSERT INTO session_segments(session_id, segment_id, revision, start_s, end_s,"
            " time_basis, lifecycle, applied_id, configuration_state, binding_revision,"
            " created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                segment.session_id, segment_id, segment.revision, segment.start_s,
                segment.end_s, segment.time_basis, segment.lifecycle.value,
                segment.applied_id, segment.configuration_state.value,
                segment.binding_revision, now, now,
            ),
        )

    def _insert_binding_row(
        self, uow: UnitOfWork, binding: ConfigurationBinding, now: str
    ) -> None:
        scope = binding.effective_range
        uow.execute(
            "INSERT INTO configuration_bindings(binding_id, segment_id, binding_revision,"
            " applied_id, configuration_state, start_s, end_s, time_basis, unit,"
            " supersedes_revision, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binding.id, binding.segment_id, binding.binding_revision,
                binding.applied_id, binding.configuration_state.value,
                scope.start, scope.end, scope.time_basis, scope.unit,
                binding.supersedes_revision, now,
            ),
        )
        uow.insert_reference(
            binding.id, "segment",
            ObjectRef(entity_type="SessionSegment", entity_id=binding.segment_id),
        )
        if binding.applied_id is not None:
            uow.insert_reference(
                binding.id, "applied",
                ObjectRef(entity_type="AppliedConfiguration", entity_id=binding.applied_id),
            )

    # -- session internals --------------------------------------------------
    def _load_session(self, uow: UnitOfWork, session_id: str) -> PlaySession:
        head = uow.get_head("PlaySession", session_id)
        if head is None:
            stored = uow.find_object("PlaySession", session_id)
            if stored is None:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the requested session is not available"
                )
            return PlaySession.model_validate_json(stored.payload_bytes)
        return PlaySession.model_validate_json(
            canonical_json(uow.get_snapshot("PlaySession", session_id, head))
        )

    def _verify_source_bytes(
        self, uow: UnitOfWork, file_id: str, content_hash: str
    ) -> None:
        stored = uow.find_object("ManagedFile", file_id)
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested file is not available"
            )
        payload = stored.payload
        stored_hash = payload.get("manifest", {}).get("sha256")
        if stored_hash != content_hash:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the log content changed since it was inspected",
            )
        if payload.get("file_status") != "published":
            raise ContractError(
                ErrorCode.ARTIFACT_UNAVAILABLE, "the log file is not consumable"
            )
        try:
            data = (self.files.root / payload["relative_path"]).read_bytes()
        except (OSError, KeyError) as error:
            raise ContractError(
                ErrorCode.ARTIFACT_UNAVAILABLE, "the stored log cannot be read"
            ) from error
        if hashlib.sha256(data).hexdigest() != stored_hash:
            raise ContractError(
                ErrorCode.ARTIFACT_UNAVAILABLE,
                "the stored log no longer matches its recorded hash",
            )

    @staticmethod
    def _require_file_owner(payload: Mapping[str, Any], user_scope_id: str) -> None:
        """A managed file is consumable only by the user whose operation wrote it."""
        if payload.get("user_scope_id") != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested file is not available"
            )

    def _require_session_owner(
        self, uow: UnitOfWork, session: PlaySession, user_scope_id: str
    ) -> None:
        stored = uow.latest_for_series("GameInstallation", session.installation_id)
        if stored is None or stored.payload.get("user_scope_id") != user_scope_id:
            # Another user's session is indistinguishable from a missing one.
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested session is not available"
            )

    def _elapsed_seconds(self, session: PlaySession, segment: SessionSegment) -> float:
        """The backend clock delta; a browser time or a guess is never used."""
        try:
            started = datetime.fromisoformat(session.started_at)
            now = datetime.fromisoformat(self.clock())
            elapsed = (now - started).total_seconds()
        except ValueError as error:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the session start has no usable backend time; the end point is not guessed",
            ) from error
        if not math.isfinite(elapsed) or elapsed <= segment.start_s:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the backend clock did not provide a reliable end point; the segment stays open",
            )
        return elapsed

    # -- lookups and shared guards -----------------------------------------
    def _installation(
        self, uow: UnitOfWork, installation_id: str, user_scope_id: str
    ) -> dict[str, Any]:
        stored = uow.latest_for_series("GameInstallation", installation_id)
        if stored is None or stored.payload.get("user_scope_id") != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the installation is not available"
            )
        return {
            "series_id": stored.series_id,
            "object_id": stored.object_id,
            "game_id": stored.payload.get("game_id"),
        }

    def _context(
        self, uow: UnitOfWork, context_id: str, installation: Mapping[str, Any]
    ) -> Any:
        stored = uow.find_object("GameContext", context_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced game context does not exist",
                field_errors=[FieldError(
                    path="game_context_id", code="reference", message="unknown context",
                )],
            )
        payload = stored.payload
        if payload.get("installation_id") != installation["series_id"]:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID, "the game context belongs to another installation"
            )
        if payload.get("game_id") != installation["game_id"]:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID, "the game context belongs to another game"
            )
        return stored

    def _device_snapshot(
        self, uow: UnitOfWork, snapshot_id: str, user_scope_id: str
    ) -> Any:
        stored = uow.find_object("DeviceSnapshot", snapshot_id)
        if stored is None or stored.payload.get("user_scope_id") != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the device snapshot is not available"
            )
        return stored

    def _applied(
        self,
        uow: UnitOfWork,
        applied_id: str | None,
        installation: Mapping[str, Any],
        user_scope_id: str,
    ) -> Mapping[str, Any] | None:
        if applied_id is None:
            return None
        stored = uow.find_object("AppliedConfiguration", applied_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID, "the referenced applied configuration does not exist"
            )
        if stored.payload.get("user_scope_id") != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the applied configuration is not available"
            )
        if stored.payload.get("installation_id") != installation["series_id"]:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the applied configuration belongs to another installation",
            )
        return stored.payload

    def _applied_by_installation(
        self, uow: UnitOfWork, applied_id: str | None, installation_id: str
    ) -> Mapping[str, Any] | None:
        if applied_id is None:
            return None
        stored = uow.find_object("AppliedConfiguration", applied_id)
        if stored is None:
            return None
        if stored.payload.get("installation_id") != installation_id:
            return None
        return stored.payload

    def _test_protocol_ref(
        self, uow: UnitOfWork, protocol_id: str | None, game_id: str
    ) -> Any:
        if protocol_id is None:
            return None
        stored = uow.find_object("TestProtocol", protocol_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID, "the referenced test protocol does not exist"
            )
        if stored.payload.get("game_id") != game_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID, "the test protocol belongs to another game"
            )
        return stored

    def _recommendation(self, uow: UnitOfWork, recommendation_id: str | None) -> None:
        if recommendation_id is None:
            return
        if uow.find_object("RecommendationVersion", recommendation_id) is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID, "the referenced recommendation does not exist"
            )

    def _require_declaration_installation(
        self, declaration: RunDeclarationInput, installation_id: str
    ) -> None:
        if declaration.installation_id != installation_id:
            raise _invalid(
                "run_declaration.installation_id", "conflict",
                "the run declaration names another installation",
            )

    @staticmethod
    def _refuse_unverifiable_configuration(
        applied_id: str | None,
        state: ConfigurationState,
        fields: Mapping[str, Scalar],
    ) -> None:
        if applied_id is not None:
            return
        if state is ConfigurationState.bound:
            raise _invalid(
                "configuration_state", "enum",
                "a session without an applied configuration cannot be bound",
            )
        if state is ConfigurationState.mixed and not fields:
            raise _invalid(
                "configuration_state", "conflict",
                "a mixed session needs a stated basis for the change",
            )

    def _evidence_problems(
        self,
        uow: UnitOfWork,
        evidence_refs: Sequence[EvidenceRef],
        user_scope_id: str,
    ) -> None:
        for ref in evidence_refs:
            if ref.kind == "artifact":
                stored = uow.find_object("ManagedFile", ref.file_id)
                if stored is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the evidence file does not exist",
                    )
                if stored.payload.get("user_scope_id") != user_scope_id:
                    raise ContractError(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        "the evidence file is not available",
                    )
                if stored.payload.get("file_status") != "published":
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the evidence file is not published",
                    )
                if stored.payload.get("manifest", {}).get("sha256") != ref.content_hash:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the evidence hash does not match the stored file",
                        field_errors=[FieldError(
                            path="evidence_refs", code="reference",
                            message="artifact content_hash does not match the managed file",
                        )],
                    )
            elif ref.kind == "source_location":
                if uow.find_object("SourceRevision", ref.source_revision_id) is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the evidence source revision does not exist",
                    )
            elif ref.kind == "local":
                if uow.find_object(ref.entity_type, ref.entity_id) is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the evidence record does not exist",
                    )
            else:
                if uow.find_object("ImportedRecord", ref.record.record_snapshot_id) is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the evidence knowledge record does not exist",
                    )

    def _write_creation_references(
        self,
        uow: UnitOfWork,
        *,
        session: PlaySession,
        segment: SessionSegment,
        declaration: PlayerDeclaration,
        installation: Mapping[str, Any],
        context: Any,
    ) -> None:
        owner = session.id
        uow.insert_reference(owner, "declaration", ObjectRef(
            entity_type="PlayerDeclaration", entity_id=declaration.id,
        ))
        uow.insert_reference(owner, "installation", ObjectRef(
            entity_type="GameInstallation", entity_id=installation["object_id"],
        ))
        uow.insert_reference(owner, "game_context", ObjectRef(
            entity_type="GameContext", entity_id=context.object_id,
        ))
        uow.insert_reference(owner, "device_snapshot", ObjectRef(
            entity_type="DeviceSnapshot", entity_id=session.device_snapshot_id,
        ))
        if session.initial_applied_id is not None:
            uow.insert_reference(owner, "applied", ObjectRef(
                entity_type="AppliedConfiguration", entity_id=session.initial_applied_id,
            ))
        if session.test_protocol_id is not None:
            uow.insert_reference(owner, "test_protocol", ObjectRef(
                entity_type="TestProtocol", entity_id=session.test_protocol_id,
            ))
        if session.recommendation_id is not None:
            uow.insert_reference(owner, "recommendation", ObjectRef(
                entity_type="RecommendationVersion", entity_id=session.recommendation_id,
            ))
        session_ref = ObjectRef(entity_type="PlaySession", entity_id=session.id)
        uow.insert_reference(segment.id, "session", session_ref)

    # -- idempotency --------------------------------------------------------
    def _has_registered(self, context: MutationContext, route: str) -> bool:
        return self.repository.find_idempotency(
            context.user_scope_id, "POST", route, context.idempotency_key
        ) is not None

    def _replay_resource(
        self,
        uow: UnitOfWork,
        context: MutationContext,
        route: str,
        request_schema_version: str,
        request_hash: str,
    ) -> ResourceResult | None:
        existing = uow.find_idempotency(
            context.user_scope_id, "POST", route, context.idempotency_key
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
                field_errors=[FieldError(
                    path="X-Product-Contract-Version", code="conflict",
                    message=f"registered as {existing.request_schema_version}",
                )],
            )
        if existing.request_hash != request_hash:
            raise ContractError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "the same key was already used with different business content",
                field_errors=[FieldError(
                    path="idempotency_key", code="conflict",
                    message="different request hash",
                )],
            )
        return ResourceResult.model_validate(json.loads(existing.response_body))

    def _record_resource(
        self,
        uow: UnitOfWork,
        context: MutationContext,
        route: str,
        request_schema_version: str,
        request_hash: str,
        result: ResourceResult,
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


# -- pure helpers -------------------------------------------------------------


@dataclass(frozen=True)
class _AnalysisTargets:
    artifact: CaptureArtifact
    session: PlaySession
    bindings: list[tuple[SessionSegment, ConfigurationBinding]]
    file_payload: Mapping[str, Any]
    data: bytes


@dataclass(frozen=True)
class _BindingCleaning:
    segment: SessionSegment
    binding: ConfigurationBinding
    window: TimeRange
    cleaning: Any


@dataclass(frozen=True)
class _AnalysisPlan:
    metrics: list[MetricValue]
    adaptive: list[AdaptiveLongFrame]
    quality: LogQuality
    checks: list[CheckResult]
    manifest: dict[str, Any]
    analyzed_ranges: list[TimeRange]
    valid_interval_count: int
    effective_duration_s: float


@dataclass(frozen=True)
class _SummaryTarget:
    summary: PerformanceSummary
    artifact: CaptureArtifact
    session: PlaySession
    bindings: list[tuple[SessionSegment, ConfigurationBinding]]
    file_payload: Mapping[str, Any] | None


@dataclass(frozen=True)
class _Recompute:
    available: bool
    intervals: list[float]
    duration_s: float
    stream_kind: FrameStreamKind | None
    reason: str | None


_QUALITY_ORDER = {
    LogQuality.invalid: 0,
    LogQuality.limited: 1,
    LogQuality.valid: 2,
}


def _worse_quality(left: LogQuality, right: LogQuality) -> LogQuality:
    return left if _QUALITY_ORDER[left] <= _QUALITY_ORDER[right] else right


def _analysis_check(
    check_id: str, status: CheckStatus, note: str, now: str
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=ANALYZER_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else now,
        method="deterministic analysis rule",
        note=note,
    )


def _seconds(value: float, unit: str) -> float:
    return value if unit == "s" else value / 1000.0


def _analysis_window(
    segment: SessionSegment, binding: ConfigurationBinding
) -> TimeRange:
    if segment.end_s is None or binding.effective_range.end is None:
        raise ContractError(
            ErrorCode.PRECONDITION_UNVERIFIED,
            "an open segment or binding cannot be analyzed",
        )
    start = max(segment.start_s, _seconds(
        binding.effective_range.start, binding.effective_range.unit
    ))
    end = min(segment.end_s, _seconds(
        binding.effective_range.end, binding.effective_range.unit
    ))
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ContractError(
            ErrorCode.REFERENCE_INVALID,
            "the named binding lies outside the named segment revision",
        )
    return TimeRange(
        start=start, end=end, time_basis=segment.time_basis, unit="s",
    )


def _unconfirmed_stream_reason(parsed: Any) -> tuple[str, str] | None:
    """Why the artifact's stream cannot carry an ``application_present`` metric.

    Returns the human note and the legacy ``NumericValueV01`` unknown reason.
    """
    if len(parsed.observed_streams) > 1:
        return (
            "the log mixes frame streams without evidence selecting one;"
            " the streams are not merged",
            "conflicting",
        )
    if parsed.frame_stream_kind is not FrameStreamKind.application_present:
        return (
            "the frame stream is not confirmed as application_present;"
            " the packaged metric definitions do not apply",
            "scope_ambiguous",
        )
    return None


def _unknown_numeric(reason: str) -> NumericValueV01:
    return NumericValueV01(
        number_kind="unknown",
        value=None,
        lower=None,
        upper=None,
        unknown_reason=reason,
    )


def _known_numeric(value: float) -> NumericValueV01:
    return NumericValueV01(
        number_kind="exact",
        value=value,
        lower=None,
        upper=None,
        unknown_reason=None,
    )


def _metric_value(
    rule: Any,
    *,
    intervals: Sequence[float],
    confirmed: bool,
    stream_unknown: str,
    stream_kind: FrameStreamKind,
    artifact_ref: ObjectRef,
) -> MetricValue:
    """Compute one packaged rule; insufficient or unconfirmed input is unknown."""
    if not confirmed:
        numeric = _unknown_numeric(stream_unknown)
    elif not intervals:
        numeric = _unknown_numeric("not_provided")
    elif rule.metric_key == "average_present_rate":
        value = mean_present_rate(intervals)
        numeric = _known_numeric(value) if value is not None else _unknown_numeric(
            "not_provided"
        )
    elif rule.metric_key == "interval_p95_nearest_rank":
        value = percentile_nearest_rank(intervals, 0.95)
        numeric = _known_numeric(value) if value is not None else _unknown_numeric(
            "not_provided"
        )
    elif rule.metric_key == "interval_p99_nearest_rank":
        value = percentile_nearest_rank(intervals, 0.99)
        numeric = _known_numeric(value) if value is not None else _unknown_numeric(
            "not_provided"
        )
    elif rule.metric_key == "slowest_1pct_inverse_mean":
        # The raw algorithm value is defined for any sample, but the project's
        # 20-sample tail gate keeps it away from the user.
        value = slowest_tail_inverse_mean(intervals)
        count = tail_sample_count(intervals)
        if value is None:
            numeric = _unknown_numeric("not_provided")
        elif count < TAIL_SAMPLE_FLOOR:
            numeric = _unknown_numeric("not_provided")
        else:
            numeric = _known_numeric(value)
    else:  # pragma: no cover - the packaged registry is fixed
        numeric = _unknown_numeric("not_provided")

    denominator = None
    if rule.metric_key == "average_present_rate" and intervals:
        total = sum(intervals) / 1000.0
        if total > 0:
            denominator = MetricDenominator(
                value=total, unit="s", input_refs=[artifact_ref],
            )
    return MetricValue(
        definition_id=_metric_definition_id(rule),
        numeric=numeric,
        unit=rule.unit,
        frame_stream_kind=stream_kind,
        input_refs=[artifact_ref],
        denominator=denominator,
        method=rule.algorithm_ref,
    )


def _adaptive_long_frame(run: ContinuousRun, segment: SessionSegment) -> AdaptiveLongFrame:
    threshold = adaptive_threshold_ms(run.intervals)
    if threshold is None:  # pragma: no cover - runs always carry intervals
        return AdaptiveLongFrame(
            range=TimeRange(
                start=run.start_s, end=run.end_s,
                time_basis=segment.time_basis, unit="s",
            ),
            threshold_ms=None,
            cluster_count=None,
            interval_count=None,
            long_frame_duration_ms=None,
            reason="the run carries no valid interval",
        )
    clusters, count, duration = long_frame_statistics(run.intervals, threshold)
    return AdaptiveLongFrame(
        range=TimeRange(
            start=run.start_s, end=run.end_s,
            time_basis=segment.time_basis, unit="s",
        ),
        threshold_ms=threshold,
        cluster_count=clusters,
        interval_count=count,
        long_frame_duration_ms=duration,
        reason=None,
    )


def _ranges_overlap(
    left: Sequence[TimeRange], right: Sequence[TimeRange]
) -> bool:
    for first in left:
        for second in right:
            if first.time_basis != second.time_basis or first.unit != second.unit:
                return True  # disjointness cannot be proven across bases
            if first.start < second.end and first.end > second.start:
                return True
    return False


def _comparison_check(
    check_id: str, status: CheckStatus, note: str, now: str
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=COMPARISON_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else now,
        method="deterministic comparison rule",
        note=note,
    )


def _compare_field(left: Any, right: Any) -> CheckStatus:
    if (
        left.value_state is ValueState.known
        and right.value_state is ValueState.known
    ):
        return CheckStatus.pass_ if left.value == right.value else CheckStatus.fail
    return CheckStatus.unknown


def _combine_statuses(statuses: Sequence[CheckStatus]) -> CheckStatus:
    if any(item is CheckStatus.fail for item in statuses):
        return CheckStatus.fail
    if any(item is CheckStatus.unknown for item in statuses):
        return CheckStatus.unknown
    return CheckStatus.pass_


def _compare_run_conditions(
    left: RunConditions, right: RunConditions
) -> list[CheckStatus]:
    statuses = [
        _compare_field(left.output_resolution, right.output_resolution),
        _compare_field(left.render_resolution, right.render_resolution),
        _compare_field(left.frame_generation, right.frame_generation),
        _compare_field(left.upscaling, right.upscaling),
    ]
    for key in sorted(set(left.scene_conditions) | set(right.scene_conditions)):
        if key not in left.scene_conditions or key not in right.scene_conditions:
            statuses.append(CheckStatus.unknown)
        else:
            statuses.append(_compare_field(
                left.scene_conditions[key], right.scene_conditions[key],
            ))
    return statuses


def _compare_stream(left: StreamSelection, right: StreamSelection) -> CheckStatus:
    if (
        left.frame_stream_kind is FrameStreamKind.unknown
        or right.frame_stream_kind is FrameStreamKind.unknown
    ):
        return CheckStatus.unknown
    return (
        CheckStatus.pass_
        if left.frame_stream_kind is right.frame_stream_kind
        else CheckStatus.fail
    )


def _protocol_metric_rules(protocol: ComparisonProtocol) -> tuple[Any, Any]:
    """Two definitions fingerprinted by the frozen protocol (04 §6 step 9)."""
    fingerprint = hashlib.sha256(canonical_json({
        "protocol_id": protocol.id,
        "threshold_ms": protocol.threshold_ms,
        "clustering_definition": protocol.clustering_definition,
        "denominator_definition": protocol.denominator_definition,
    })).hexdigest()[:16]
    version = f"v1-{fingerprint}"
    rate = metrics.MetricRule(
        metric_key="long_frame_cluster_rate",
        definition_version=version,
        label="Long-frame clusters per minute",
        unit="clusters/min",
        frame_stream_kind=FrameStreamKind.application_present,
        algorithm_ref=(
            f"04 §6 step 7: 60*K/T with the frozen threshold"
            f" {protocol.threshold_ms}ms and denominator T in seconds"
        ),
        minimum_sample_rule="T > 0; otherwise the value stays unknown",
    )
    share = metrics.MetricRule(
        metric_key="long_frame_time_share",
        definition_version=version,
        label="Long-frame time share",
        unit="ratio",
        frame_stream_kind=FrameStreamKind.application_present,
        algorithm_ref=(
            f"04 §6 step 7: sum(L)/(1000*T) with the frozen threshold"
            f" {protocol.threshold_ms}ms over the full interval duration"
        ),
        minimum_sample_rule="T > 0; otherwise the value stays unknown",
    )
    return rate, share


def _delta_value(
    left: MetricValue, value: float, left_ref: ObjectRef, right_ref: ObjectRef
) -> MetricValue:
    return MetricValue(
        definition_id=left.definition_id,
        numeric=_known_numeric(value),
        unit=left.unit,
        frame_stream_kind=left.frame_stream_kind,
        input_refs=[left_ref, right_ref],
        denominator=None,
        method="right-left in the definition's unit",
    )


def _compare_stored_metric(
    left: MetricValue,
    right: MetricValue | None,
    left_ref: ObjectRef,
    right_ref: ObjectRef,
) -> MetricComparison:
    if right is None:
        return MetricComparison(
            metric_definition_id=left.definition_id,
            status=CheckStatus.not_applicable,
            left=left,
            right=None,
            delta=None,
            reason="the right summary carries no metric with this definition",
        )
    left_known = left.numeric.number_kind != "unknown"
    right_known = right.numeric.number_kind != "unknown"
    if left_known and right_known:
        return MetricComparison(
            metric_definition_id=left.definition_id,
            status=CheckStatus.pass_,
            left=left,
            right=right,
            delta=_delta_value(
                left, right.numeric.value - left.numeric.value, left_ref, right_ref,
            ),
            reason=None,
        )
    reasons = [item for item in (
        None if left_known else f"left={left.numeric.unknown_reason}",
        None if right_known else f"right={right.numeric.unknown_reason}",
    ) if item]
    return MetricComparison(
        metric_definition_id=left.definition_id,
        status=CheckStatus.unknown,
        left=left,
        right=right,
        delta=None,
        reason="a side has no numeric value: " + ", ".join(reasons),
    )


def _protocol_long_frame_value(
    rule: Any,
    target: _SummaryTarget,
    recomputed: _Recompute,
    protocol: ComparisonProtocol,
    *,
    kind: str,
) -> MetricValue:
    summary_ref = ObjectRef(
        entity_type="PerformanceSummary", entity_id=target.summary.id
    )
    definition_id = _metric_definition_id(rule)
    duration = target.summary.effective_duration_s
    if not recomputed.available or duration <= 0:
        return MetricValue(
            definition_id=definition_id,
            numeric=_unknown_numeric("not_provided"),
            unit=rule.unit,
            frame_stream_kind=FrameStreamKind.application_present,
            input_refs=[summary_ref],
            denominator=None,
            method=rule.algorithm_ref,
        )
    clusters, _, long_ms = long_frame_statistics(
        recomputed.intervals, protocol.threshold_ms
    )
    value = (
        60.0 * clusters / duration
        if kind == "rate"
        else long_ms / (1000.0 * duration)
    )
    return MetricValue(
        definition_id=definition_id,
        numeric=_known_numeric(value),
        unit=rule.unit,
        frame_stream_kind=FrameStreamKind.application_present,
        input_refs=[summary_ref],
        denominator=MetricDenominator(
            value=duration, unit="s", input_refs=[summary_ref],
        ),
        method=rule.algorithm_ref,
    )


def _compare_long_frame(
    rule: Any,
    protocol: ComparisonProtocol,
    left: _SummaryTarget,
    right: _SummaryTarget,
    recomputed_left: _Recompute,
    recomputed_right: _Recompute,
    left_ref: ObjectRef,
    right_ref: ObjectRef,
    *,
    kind: str,
) -> MetricComparison:
    left_value = _protocol_long_frame_value(
        rule, left, recomputed_left, protocol, kind=kind,
    )
    right_value = _protocol_long_frame_value(
        rule, right, recomputed_right, protocol, kind=kind,
    )
    definition_id = _metric_definition_id(rule)
    if (
        left_value.numeric.number_kind != "unknown"
        and right_value.numeric.number_kind != "unknown"
    ):
        return MetricComparison(
            metric_definition_id=definition_id,
            status=CheckStatus.pass_,
            left=left_value,
            right=right_value,
            delta=_delta_value(
                left_value,
                right_value.numeric.value - left_value.numeric.value,
                left_ref, right_ref,
            ),
            reason=None,
        )
    reasons = [
        item for item in (
            None if left_value.numeric.number_kind != "unknown"
            else f"left: {recomputed_left.reason or 'no effective duration'}",
            None if right_value.numeric.number_kind != "unknown"
            else f"right: {recomputed_right.reason or 'no effective duration'}",
        ) if item
    ]
    return MetricComparison(
        metric_definition_id=definition_id,
        status=CheckStatus.unknown,
        left=left_value,
        right=right_value,
        delta=None,
        reason="the threshold metric cannot be recomputed: " + "; ".join(reasons),
    )


def _compare_long_frame_rate(*args: Any, **kwargs: Any) -> MetricComparison:
    return _compare_long_frame(*args, kind="rate", **kwargs)


def _compare_long_frame_share(*args: Any, **kwargs: Any) -> MetricComparison:
    return _compare_long_frame(*args, kind="share", **kwargs)




def _capture_check(
    check_id: str, status: CheckStatus, note: str, now: str
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=CAPTURE_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else now,
        method="deterministic capture rule",
        note=note,
    )


def _capture_offset(
    session: PlaySession, handle: CaptureHandle
) -> float | None:
    """The output's zero is the backend-measured tool start, never a guess."""
    try:
        started = datetime.fromisoformat(handle.started_at)
        session_started = datetime.fromisoformat(session.started_at)
    except ValueError:
        return None
    offset = (started - session_started).total_seconds()
    if not math.isfinite(offset) or offset < 0:
        return None
    return offset


def _capture_time_mapping(
    parsed: LogParseResult, offset: float | None, now: str
) -> TimeMapping:
    scale = parsed.time_mapping.scale
    return TimeMapping(
        source_basis=parsed.time_mapping.source_basis,
        target_basis=LOG_TARGET_TIME_BASIS,
        source_unit=parsed.time_mapping.source_unit,
        target_unit="s",
        offset=offset if scale is not None else None,
        scale=scale,
        checks=[_capture_check(
            "time.origin",
            CheckStatus.pass_ if offset is not None and scale is not None
            else CheckStatus.unknown,
            "the output's zero is mapped from the backend tool start"
            if offset is not None and scale is not None else
            "the output's clock cannot be mapped without a verified unit/origin",
            now,
        )],
    )


def _capture_stream(parsed: LogParseResult, now: str) -> StreamSelection:
    process = parsed.frames[0].process if parsed.frames else None
    return StreamSelection(
        process=process,
        swapchain_id=None,
        frame_stream_kind=parsed.frame_stream_kind,
        checks=[_capture_check(
            "segment.stream",
            CheckStatus.pass_ if parsed.observed_streams else CheckStatus.unknown,
            "the captured output states its frame stream"
            if parsed.observed_streams else
            "the captured output does not state a frame stream",
            now,
        )],
    )


def _unconfirmed_stream(now: str) -> StreamSelection:    return StreamSelection(
        process=None,
        swapchain_id=None,
        frame_stream_kind=FrameStreamKind.unknown,
        checks=[_check(
            "segment.stream", CheckStatus.not_run,
            "no capture stream is selected for this segment yet", now,
        )],
    )


def _check_with_evidence(
    check_id: str,
    status: CheckStatus,
    note: str,
    now: str,
    evidence_refs: Sequence[EvidenceRef],
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=LOG_ASSOCIATION_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=list(evidence_refs),
        checked_at=None if status is CheckStatus.not_run else now,
        method="deterministic log association rule",
        note=note,
    )


def _mapping_values(mapping: TimeMapping) -> dict[str, Any]:
    return {
        "source_basis": mapping.source_basis,
        "target_basis": mapping.target_basis,
        "source_unit": mapping.source_unit,
        "target_unit": mapping.target_unit,
        "offset": mapping.offset,
        "scale": mapping.scale,
    }


def _artifact_evidence(payload: Mapping[str, Any]) -> EvidenceRef:
    manifest = payload["manifest"]
    return EvidenceRef(
        kind="artifact",
        file_id=payload["id"],
        content_hash=manifest["sha256"],
        locator=manifest["relative_name"],
    )


def _stream_candidates(
    parsed: LogParseResult, content_hash: str
) -> list[LogStreamCandidate]:
    """Name each observed process/swapchain/stream from the parsed bytes.

    A process that was not identified stays null; no PID is invented and two
    processes are never merged into one candidate.
    """
    groups: dict[tuple[int, str | None], Any] = {}
    for frame in parsed.frames:
        groups.setdefault((frame.process.pid, frame.swapchain_id), frame.process)
    if not groups:
        return []
    observations = list(parsed.observed_streams) or [None]
    candidates: list[LogStreamCandidate] = []
    for (pid, swapchain), process in sorted(
        groups.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        for observation in observations:
            suffix = observation.stream_key if observation is not None else "unstated"
            # The parser downgrades the resolved stream to unknown when frame
            # generation is unknown or active.  A per-row observation must not
            # upgrade that: it is not evidence for an application_present claim.
            kind = parsed.frame_stream_kind
            if (
                observation is not None
                and parsed.frame_stream_kind is not FrameStreamKind.unknown
            ):
                kind = observation.frame_stream_kind
            candidates.append(LogStreamCandidate(
                stream_key=f"{parsed.format_id}:{content_hash[:16]}:{pid}:{swapchain or '-'}:{suffix}",
                process=process,
                swapchain_id=swapchain,
                frame_stream_kind=kind,
            ))
    return candidates


def _metric_definition_id(rule: metrics.MetricRule) -> str:
    return uuid5(_METRIC_ID_NAMESPACE, ":".join((
        metrics.REGISTRY_VERSION, rule.key, rule.rule_hash,
    ))).hex


def _check_condition_types(fields: Mapping[str, Scalar], path: str) -> None:
    for key, partner in _PAIR_WIDTHS.items():
        if (key in fields) != (partner in fields):
            raise _invalid(
                path, "required",
                f"{key} and {partner} must be declared together",
            )
    for key in ("output_width", "output_height", "render_width", "render_height"):
        if key in fields and (type(fields[key]) is not int or fields[key] <= 0):
            raise _invalid(path, "range", f"{key} must be a positive integer")
    if "frame_generation" in fields and type(fields["frame_generation"]) is not bool:
        raise _invalid(path, "type", "frame_generation must be a boolean")
    if "upscaling" in fields and (
        type(fields["upscaling"]) is not str or not fields["upscaling"].strip()
    ):
        raise _invalid(path, "type", "upscaling must be a non-blank string")


def _validate_condition_fields(
    fields: Mapping[str, Scalar], path: str, *, allow_scenario: bool
) -> None:
    allowed = _FIXED_CONDITIONS if allow_scenario else _PROTOCOL_CONDITIONS
    for key in fields:
        if key in allowed:
            continue
        if key.startswith("scene.") and key[len("scene."):].strip():
            continue
        raise _invalid(path, "enum", f"field {key!r} is not an allowed condition")
    _check_condition_types(fields, path)
    if "scenario" in fields:
        if not allow_scenario:
            raise _invalid(path, "enum", "scenario is a separate field, not a condition")
        if type(fields["scenario"]) is not str or not fields["scenario"].strip():
            raise _invalid(path, "type", "scenario must be a non-blank string")


def _build_run_conditions(
    fields: Mapping[str, Scalar], evidence: EvidenceRef, now: str
) -> tuple[RunConditions, FieldValue[str]]:
    _validate_condition_fields(fields, "run_declaration.fields", allow_scenario=True)

    def resolution(prefix: str) -> FieldValue[Resolution]:
        if f"{prefix}_width" in fields:
            return _known(
                Resolution,
                Resolution(width=fields[f"{prefix}_width"], height=fields[f"{prefix}_height"]),
                evidence, now,
            )
        return _unknown(Resolution)

    scene = {
        key: _known(Scalar, value, evidence, now)
        for key, value in fields.items()
        if key.startswith("scene.") and key[len("scene."):].strip()
    }
    conditions = RunConditions(
        output_resolution=resolution("output"),
        render_resolution=resolution("render"),
        frame_generation=(
            _known(bool, fields["frame_generation"], evidence, now)
            if "frame_generation" in fields else _unknown(bool)
        ),
        upscaling=(
            _known(str, fields["upscaling"], evidence, now)
            if "upscaling" in fields else _unknown(str)
        ),
        scene_conditions=scene,
    )
    scenario = (
        _known(str, fields["scenario"], evidence, now)
        if "scenario" in fields else _unknown(str)
    )
    return conditions, scenario


__all__ = ["PerformanceService", "SESSION_RULE_VERSION"]
