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
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID, uuid5

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
    ConfigurationState,
    FrameStreamKind,
    SessionLifecycle,
    ValueState,
)
from game_app.contracts.performance import (
    BindConfigurationRequest,
    ConfigurationBinding,
    CreateSessionRequest,
    EndSessionRequest,
    EndSegmentRequest,
    PlaySession,
    RunConditions,
    RunDeclarationInput,
    SessionSegment,
    SplitSegmentRequest,
    StreamSelection,
)
from game_app.contracts.primitives import (
    BindingRange,
    BindingRef,
    ObjectRef,
    Scalar,
    utc_now,
)
from game_app.errors import ContractError, ErrorCode, FieldError, empty_details
from game_app.files import FileService, PublishHooks
from game_app.repository import ProductRepository, UnitOfWork, canonical_json

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
SESSION_RULE_VERSION = "performance-session/0.4"
SESSION_ROUTE = "/api/play-sessions"
SESSION_END_ROUTE = "/api/play-sessions/{session_id}/end"
SEGMENT_END_ROUTE = "/api/session-segments/{segment_id}/end"
SEGMENT_SPLIT_ROUTE = "/api/session-segments/{segment_id}/split"
SEGMENT_BIND_ROUTE = "/api/session-segments/{segment_id}/bindings"
SESSION_TIME_BASIS = "session"

_ID_NAMESPACE = UUID("9c3d7b0e-6a24-4f18-9e53-1b7c2d5a4e60")

_PAIR_WIDTHS = {"output_width": "output_height", "output_height": "output_width",
                "render_width": "render_height", "render_height": "render_width"}
_FIXED_CONDITIONS = frozenset({
    "output_width", "output_height", "render_width", "render_height",
    "frame_generation", "upscaling", "scenario",
})


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
    ) -> None:
        self.repository = repository
        self.files = files
        self.clock = clock

    # -- reads --------------------------------------------------------------
    def get_session(self, session_id: str) -> PlaySession:
        with self.repository.unit_of_work() as uow:
            return self._load_session(uow, session_id)

    def get_segment(self, segment_id: str) -> SessionSegment:
        with self.repository.unit_of_work() as uow:
            return self._load_segment(uow, segment_id)

    def get_binding(self, segment_id: str, binding_revision: int) -> ConfigurationBinding:
        with self.repository.unit_of_work() as uow:
            return self._load_binding(uow, segment_id, binding_revision)

    def require_analyzeable(self, binding_ref: BindingRef) -> None:
        """Only a closed segment and a closed binding revision may be analyzed."""
        with self.repository.unit_of_work() as uow:
            segment = self._load_segment(uow, binding_ref.segment_id)
            self._require_current_revisions(
                segment,
                expected_segment_revision=binding_ref.segment_revision,
                expected_binding_revision=binding_ref.binding_revision,
            )
            if segment.lifecycle is not SessionLifecycle.ended:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "an open segment cannot be analyzed; end it first",
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
            self._refuse_unverifiable_configuration(
                request.new_applied_id, request.configuration_state,
                request.run_declaration.fields,
            )
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
            self._evidence_problems(uow, request.evidence_refs)
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

    # -- segment internals --------------------------------------------------
    def _segment_and_session(
        self, uow: UnitOfWork, segment_id: str, user_scope_id: str
    ) -> tuple[SessionSegment, PlaySession]:
        segment = self._load_segment(uow, segment_id)
        session = self._load_session(uow, segment.session_id)
        self._require_session_owner(uow, session, user_scope_id)
        return segment, session

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
        if not math.isfinite(end_s) or end_s <= segment.start_s:
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
            evidence_refs=[],
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
        self, uow: UnitOfWork, evidence_refs: Sequence[EvidenceRef]
    ) -> None:
        for ref in evidence_refs:
            if ref.kind == "artifact":
                stored = uow.find_object("ManagedFile", ref.file_id)
                if stored is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the evidence file does not exist",
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


def _unconfirmed_stream(now: str) -> StreamSelection:
    return StreamSelection(
        process=None,
        swapchain_id=None,
        frame_stream_kind=FrameStreamKind.unknown,
        checks=[_check(
            "segment.stream", CheckStatus.not_run,
            "no capture stream is selected for this segment yet", now,
        )],
    )


def _build_run_conditions(
    fields: Mapping[str, Scalar], evidence: EvidenceRef, now: str
) -> tuple[RunConditions, FieldValue[str]]:
    for key in fields:
        if key in _FIXED_CONDITIONS:
            continue
        if key.startswith("scene.") and key[len("scene."):].strip():
            continue
        raise _invalid("run_declaration.fields", "enum", f"field {key!r} is not a run condition")

    for key, partner in _PAIR_WIDTHS.items():
        if (key in fields) != (partner in fields):
            raise _invalid(
                "run_declaration.fields", "required",
                f"{key} and {partner} must be declared together",
            )
    for key in ("output_width", "output_height", "render_width", "render_height"):
        if key in fields and (type(fields[key]) is not int or fields[key] <= 0):
            raise _invalid(
                "run_declaration.fields", "range",
                f"{key} must be a positive integer",
            )
    if "frame_generation" in fields and type(fields["frame_generation"]) is not bool:
        raise _invalid(
            "run_declaration.fields", "type", "frame_generation must be a boolean",
        )
    if "upscaling" in fields and (
        type(fields["upscaling"]) is not str or not fields["upscaling"].strip()
    ):
        raise _invalid(
            "run_declaration.fields", "type", "upscaling must be a non-blank string",
        )
    if "scenario" in fields and (
        type(fields["scenario"]) is not str or not fields["scenario"].strip()
    ):
        raise _invalid("run_declaration.fields", "type", "scenario must be a non-blank string")

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
