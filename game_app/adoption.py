"""Configuration adoption and historical registration (01 §5.2, state §4).

The service records what the player declared.  It never writes a game file,
never probes, and never turns a declaration into a ``system_probe``.  A current
adoption advances the per-installation pointer in the same short transaction as
its declaration; a historical record does not touch that pointer at all.
"""

from __future__ import annotations

import hashlib
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
from game_app.contracts.configuration import (
    AdoptConfigurationInput,
    AppliedConfiguration,
    ConfirmedSetting,
    HistoricalConfigurationInput,
    PlayerDeclaration,
    SettingDeclaration,
    SettingRef,
    SettingRefReplacement,
)
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    ActivationState,
    AppliedOrigin,
    CheckStatus,
    ConfigurationRegistrationKind,
    ValueState,
)
from game_app.contracts.primitives import ObjectRef, utc_now
from game_app.contracts.knowledge import ImportedRecord
from game_app.errors import ContractError, ErrorCode, FieldError, empty_details
from game_app.files import FileService, PublishHooks
from game_app.repository import ProductRepository, StoredObject, UnitOfWork, canonical_json

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
ADOPTION_ROUTE = "/api/applied-configurations"
HISTORICAL_ROUTE = "/api/historical-configurations"
ADOPTION_RULE_VERSION = "configuration-adoption/0.4"
_ID_NAMESPACE = UUID("5b9d3c72-1a4e-4f80-9c61-2d7e8a90b1c2")


def _deterministic_id(*parts: str) -> str:
    return uuid5(_ID_NAMESPACE, ":".join(parts)).hex


def _check(check_id: str, status: CheckStatus, note: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=ADOPTION_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else utc_now(),
        method="deterministic adoption rule",
        note=note,
    )


class AdoptionService:
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
    def get_current_applied(
        self, user_scope_id: str, installation_id: str
    ) -> tuple[str | None, int]:
        with self.repository.unit_of_work() as uow:
            row = uow.current_applied(user_scope_id, installation_id)
        if row is None:
            return None, 0
        return row["applied_id"], int(row["revision"])

    def get_applied(self, applied_id: str) -> AppliedConfiguration:
        stored = self.repository.get_object("AppliedConfiguration", applied_id)
        return AppliedConfiguration.model_validate_json(stored.payload_bytes)

    # -- current adoption ---------------------------------------------------
    def adopt_configuration(
        self,
        request: AdoptConfigurationInput,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": "POST",
            "route": ADOPTION_ROUTE,
            "request": request.model_dump(mode="json"),
        })).hexdigest()
        # A replay of a registered key returns its original response; the new
        # submission's guards run only when this key has no record yet.
        if not self._has_registered(context, ADOPTION_ROUTE):
            self._validate_links(request)
            self._require_at_least_one_confirmation(
                request.declared_settings, request.preset_reference, request.origin
            )
            self._validate_references(request, context.user_scope_id)

        ids = {
            role: _deterministic_id(
                context.user_scope_id, ADOPTION_ROUTE, context.idempotency_key,
                request_hash, role,
            )
            for role in ("applied", "series", "declaration")
        }
        manifest = {
            "kind": "configuration_adoption",
            "rule_version": ADOPTION_RULE_VERSION,
            "request": request.model_dump(mode="json"),
            "applied_configuration_id": ids["applied"],
            "declaration_id": ids["declaration"],
            "series_id": ids["series"],
        }
        published = self.files.publish(
            kind="import",
            files={f"adoptions/{ids['applied']}/declaration.json": canonical_json(manifest)},
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=ADOPTION_ROUTE,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_adoption(
                uow,
                request=request,
                user_scope_id=context.user_scope_id,
                ids=ids,
                managed=managed,
            ),
            hooks=hooks,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _write_adoption(
        self,
        uow: UnitOfWork,
        *,
        request: AdoptConfigurationInput,
        user_scope_id: str,
        ids: Mapping[str, str],
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        installation = self._installation(uow, request.installation_id, user_scope_id)
        self._context(uow, request.game_context_id, installation)
        self._device_snapshot(uow, request.device_snapshot_id)
        self._validate_links(request)

        pointer = uow.current_applied(user_scope_id, installation["series_id"])
        current_id = None if pointer is None else pointer["applied_id"]
        if request.expected_current_applied_id != current_id:
            raise ContractError(
                ErrorCode.CURRENT_CONFIGURATION_CONFLICT,
                "the current configuration changed; re-read it before adopting",
                field_errors=[FieldError(
                    path="expected_current_applied_id", code="conflict",
                    message="not the current adopted configuration",
                )],
                details=empty_details(
                    current_resource_ref=(
                        None if current_id is None
                        else ObjectRef(entity_type="AppliedConfiguration", entity_id=current_id)
                    )
                ),
            )
        current = self._load_applied(uow, current_id) if current_id else None
        evidence = self._declaration_evidence(managed[0])

        settings, base_id, restored_from, merge_checks = self._compose_settings(
            uow, request, current, installation, evidence
        )
        self._refuse_series_duplicates(uow, settings)
        if not settings and not request.preset_reference:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "an adopted configuration needs a confirmed setting or an explicit preset",
                field_errors=[FieldError(
                    path="declared_settings", code="required",
                    message="no confirmed setting and no preset reference",
                )],
            )

        now = self.clock()
        declaration = PlayerDeclaration(
            id=ids["declaration"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            installation_id=installation["series_id"],
            input=request,
            raw_text="",
            submitted_at=now,
            declaration_file_id=managed[0].id,
        )
        applied = AppliedConfiguration(
            id=ids["applied"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            series_id=current.series_id if current is not None else ids["series"],
            version=current.version + 1 if current is not None else 1,
            user_scope_id=user_scope_id,
            installation_id=installation["series_id"],
            game_id=installation["game_id"],
            registration_kind=ConfigurationRegistrationKind.current_adoption,
            device_snapshot_id=request.device_snapshot_id,
            game_context_id=request.game_context_id,
            origin=request.origin,
            recommendation_id=request.recommendation_id,
            parent_applied_id=base_id,
            change_set_id=request.change_set_id,
            restored_from_applied_id=restored_from,
            settings=settings,
            preset_reference=request.preset_reference,
            adopted_at=now,
            activation_state=_reduce_activation(settings, request.claimed_activation),
            checks=[
                _check(
                    "applied.source",
                    CheckStatus.not_run,
                    "a player declaration is not a program observation and no game file was written",
                ),
                *merge_checks,
            ],
            declaration_id=declaration.id,
        )
        uow.insert_object("PlayerDeclaration", declaration)
        uow.insert_versioned("AppliedConfiguration", applied)
        self._write_references(uow, applied, declaration)
        uow.advance_current_applied(
            user_scope_id=user_scope_id,
            installation_id=installation["series_id"],
            expected_applied_id=request.expected_current_applied_id,
            next_applied_id=applied.id,
            now=now,
        )
        return [
            ObjectRef(entity_type="PlayerDeclaration", entity_id=declaration.id),
            ObjectRef(entity_type="AppliedConfiguration", entity_id=applied.id),
            ObjectRef(entity_type="ManagedFile", entity_id=managed[0].id),
        ]

    # -- historical registration -------------------------------------------
    def record_historical_configuration(
        self,
        request: HistoricalConfigurationInput,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": "POST",
            "route": HISTORICAL_ROUTE,
            "request": request.model_dump(mode="json"),
        })).hexdigest()
        if not self._has_registered(context, HISTORICAL_ROUTE):
            self._require_at_least_one_confirmation(
                request.declared_settings, request.preset_reference, AppliedOrigin.baseline
            )
            self._validate_references(request, context.user_scope_id)

        ids = {
            role: _deterministic_id(
                context.user_scope_id, HISTORICAL_ROUTE, context.idempotency_key,
                request_hash, role,
            )
            for role in ("applied", "series", "declaration")
        }
        manifest = {
            "kind": "historical_configuration",
            "rule_version": ADOPTION_RULE_VERSION,
            "request": request.model_dump(mode="json"),
            "applied_configuration_id": ids["applied"],
            "declaration_id": ids["declaration"],
            "series_id": ids["series"],
            "segment_ref": request.segment_ref.model_dump(mode="json"),
            "effective_range": request.effective_range.model_dump(mode="json"),
        }
        published = self.files.publish(
            kind="import",
            files={
                f"historical-configurations/{ids['applied']}/declaration.json":
                    canonical_json(manifest)
            },
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=HISTORICAL_ROUTE,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_historical(
                uow,
                request=request,
                user_scope_id=context.user_scope_id,
                ids=ids,
                managed=managed,
            ),
            hooks=hooks,
        )
        return ResourceResult(refs=[ObjectRef.model_validate(ref) for ref in published.refs])

    def _write_historical(
        self,
        uow: UnitOfWork,
        *,
        request: HistoricalConfigurationInput,
        user_scope_id: str,
        ids: Mapping[str, str],
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        installation = self._installation(uow, request.installation_id, user_scope_id)
        self._context(uow, request.game_context_id, installation)
        self._device_snapshot(uow, request.device_snapshot_id)
        # A historical record must not be a way to smuggle in today's state.
        self._evidence_problems(uow, request.evidence_refs)
        segment_check = self._segment_check(uow, request)

        now = self.clock()
        evidence = self._declaration_evidence(managed[0])
        settings = [
            _confirm(declaration, evidence, now)
            for declaration in request.declared_settings
        ]
        self._refuse_series_duplicates(uow, settings)
        if not settings and not request.preset_reference:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a historical record needs a confirmed setting or an explicit preset",
            )
        declaration = PlayerDeclaration(
            id=ids["declaration"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            installation_id=installation["series_id"],
            input=request,
            raw_text="",
            submitted_at=now,
            declaration_file_id=managed[0].id,
        )
        applied = AppliedConfiguration(
            id=ids["applied"],
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=now,
            created_by_task_id=None,
            series_id=ids["series"],
            version=1,
            user_scope_id=user_scope_id,
            installation_id=installation["series_id"],
            game_id=installation["game_id"],
            registration_kind=ConfigurationRegistrationKind.historical_record,
            device_snapshot_id=request.device_snapshot_id,
            game_context_id=request.game_context_id,
            origin=AppliedOrigin.baseline,
            recommendation_id=None,
            parent_applied_id=None,
            change_set_id=None,
            restored_from_applied_id=None,
            settings=settings,
            preset_reference=request.preset_reference,
            adopted_at=request.adopted_at,
            activation_state=_reduce_activation(settings, request.claimed_activation),
            checks=[
                _check(
                    "applied.source",
                    CheckStatus.not_run,
                    "a historical registration records a past declaration, not an observation",
                ),
                _check(
                    "applied.period",
                    CheckStatus.pass_,
                    "the declared time period is backed by located evidence",
                ),
                _check(
                    "applied.period_coverage",
                    CheckStatus.unknown,
                    "nothing in the submitted evidence proves it covers the whole"
                    " declared period; the registration keeps that limit",
                ),
                segment_check,
            ],
            declaration_id=declaration.id,
        )
        uow.insert_object("PlayerDeclaration", declaration)
        uow.insert_versioned("AppliedConfiguration", applied)
        self._write_references(uow, applied, declaration)
        return [
            ObjectRef(entity_type="PlayerDeclaration", entity_id=declaration.id),
            ObjectRef(entity_type="AppliedConfiguration", entity_id=applied.id),
            ObjectRef(entity_type="ManagedFile", entity_id=managed[0].id),
        ]

    # -- guards -------------------------------------------------------------
    @staticmethod
    def _validate_links(request: AdoptConfigurationInput) -> None:
        """The origin table from state §4.1; mixing origins is refused."""
        origin = request.origin
        forbidden: list[tuple[str, Any]] = []
        required: list[tuple[str, Any]] = []
        if origin is AppliedOrigin.baseline:
            forbidden = [
                ("recommendation_id", request.recommendation_id),
                ("change_set_id", request.change_set_id),
                ("restored_from_applied_id", request.restored_from_applied_id),
            ]
        elif origin is AppliedOrigin.recommendation:
            required = [("recommendation_id", request.recommendation_id)]
            forbidden = [
                ("change_set_id", request.change_set_id),
                ("restored_from_applied_id", request.restored_from_applied_id),
            ]
        elif origin is AppliedOrigin.change_set:
            required = [("change_set_id", request.change_set_id)]
            forbidden = [
                ("restored_from_applied_id", request.restored_from_applied_id),
            ]
        elif origin is AppliedOrigin.manual_edit:
            forbidden = [
                ("change_set_id", request.change_set_id),
                ("restored_from_applied_id", request.restored_from_applied_id),
            ]
        elif origin is AppliedOrigin.rollback:
            required = [
                ("restored_from_applied_id", request.restored_from_applied_id),
            ]
            forbidden = [("change_set_id", request.change_set_id)]
        for field_name, value in required:
            if value is None:
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    f"origin {origin.value} requires {field_name}",
                    field_errors=[FieldError(
                        path=field_name, code="required",
                        message=f"required for origin {origin.value}",
                    )],
                )
        for field_name, value in forbidden:
            if value is not None:
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    f"origin {origin.value} must not carry {field_name}",
                    field_errors=[FieldError(
                        path=field_name, code="conflict",
                        message=f"not allowed for origin {origin.value}",
                    )],
                )
        if origin is not AppliedOrigin.change_set:
            if request.base_applied_id is not None and (
                request.base_applied_id != request.expected_current_applied_id
            ):
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    "for this origin the base is the current adopted configuration",
                    field_errors=[FieldError(
                        path="base_applied_id", code="conflict",
                        message="differs from expected_current_applied_id",
                    )],
                )

    @staticmethod
    def _require_at_least_one_confirmation(
        declarations: Sequence[SettingDeclaration],
        preset_reference: str | None,
        origin: AppliedOrigin,
    ) -> None:
        if origin is AppliedOrigin.rollback:
            return
        confirmed = any(declaration.value is not None for declaration in declarations)
        if not confirmed and preset_reference is None:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "at least one setting must be confirmed or an explicit preset named",
                field_errors=[FieldError(
                    path="declared_settings", code="required",
                    message="no confirmed setting and no preset reference",
                )],
            )

    def _validate_references(
        self, request: AdoptConfigurationInput | HistoricalConfigurationInput, user_scope_id: str
    ) -> None:
        with self.repository.unit_of_work() as uow:
            installation = self._installation(
                uow, request.installation_id, user_scope_id
            )
            self._context(uow, request.game_context_id, installation)
            self._device_snapshot(uow, request.device_snapshot_id)
            for declaration in request.declared_settings:
                self._setting_ref(uow, declaration.setting_ref, installation["series_id"])
            if isinstance(request, HistoricalConfigurationInput):
                self._evidence_problems(uow, request.evidence_refs)

    # -- composition --------------------------------------------------------
    def _compose_settings(
        self,
        uow: UnitOfWork,
        request: AdoptConfigurationInput,
        current: AppliedConfiguration | None,
        installation: Mapping[str, Any],
        evidence: EvidenceRef,
    ) -> tuple[list[ConfirmedSetting], str | None, str | None, list[CheckResult]]:
        origin = request.origin
        now = self.clock()
        checks: list[CheckResult] = []

        if origin is AppliedOrigin.baseline:
            return (
                [_confirm(item, evidence, now) for item in request.declared_settings],
                current.id if current is not None else None,
                None,
                checks,
            )
        if origin is AppliedOrigin.recommendation:
            self._recommendation(uow, request.recommendation_id)
            return (
                [_confirm(item, evidence, now) for item in request.declared_settings],
                current.id if current is not None else None,
                None,
                checks,
            )
        if origin is AppliedOrigin.manual_edit:
            if current is None:
                raise ContractError(
                    ErrorCode.BASELINE_REQUIRED,
                    "a manual edit needs a current configuration; record a baseline first",
                )
            settings = self._manual_edit(uow, request, current, installation, evidence, now)
            return settings, current.id, None, checks
        if origin is AppliedOrigin.rollback:
            if current is None:
                raise ContractError(
                    ErrorCode.BASELINE_REQUIRED,
                    "there is no current configuration to restore from",
                )
            target = self._load_applied(uow, request.restored_from_applied_id)
            if (
                target.user_scope_id != current.user_scope_id
                or target.installation_id != current.installation_id
            ):
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the restore target belongs to another user or installation",
                )
            settings = list(target.settings)
            if request.declared_settings:
                settings = _overlay(
                    settings,
                    [_confirm(item, evidence, now) for item in request.declared_settings],
                )
            checks.append(_check(
                "applied.rollback",
                CheckStatus.pass_,
                f"a new version restores the immutable snapshot {target.id};"
                " the old id is never reused",
            ))
            return settings, current.id, target.id, checks

        # origin == change_set
        change_set, base_id = self._change_set(uow, request)
        if current is None or current.id != base_id:
            raise ContractError(
                ErrorCode.CURRENT_CONFIGURATION_CONFLICT,
                "the ChangeSet base is not the current adopted configuration",
                field_errors=[FieldError(
                    path="base_applied_id", code="conflict",
                    message="no longer the current configuration",
                )],
            )
        base = self._load_applied(uow, base_id)
        merged = list(base.settings)
        merged = _overlay(
            merged,
            [_confirm(item, evidence, now) for item in request.declared_settings],
        )
        self._verify_changes(merged, change_set)
        checks.append(_check(
            "applied.merge",
            CheckStatus.pass_,
            "merged from the frozen ChangeSet base snapshot; unchanged, unknown and custom"
            " entries were preserved",
        ))
        return merged, base_id, None, checks

    def _manual_edit(
        self,
        uow: UnitOfWork,
        request: AdoptConfigurationInput,
        current: AppliedConfiguration,
        installation: Mapping[str, Any],
        evidence: EvidenceRef,
        now: str,
    ) -> list[ConfirmedSetting]:
        base = list(current.settings)
        self._validate_replacements(
            uow, request.setting_ref_replacements, request.declared_settings, base,
            installation["series_id"],
        )
        removed = {item.old_local_setting_id for item in request.setting_ref_replacements}
        kept = [
            setting for setting in base
            if not (
                setting.setting_ref.kind == "local"
                and setting.setting_ref.local_setting_id in removed
            )
        ]
        return _overlay(
            kept, [_confirm(item, evidence, now) for item in request.declared_settings]
        )

    def _validate_replacements(
        self,
        uow: UnitOfWork,
        replacements: Sequence[SettingRefReplacement],
        declarations: Sequence[SettingDeclaration],
        base: Sequence[ConfirmedSetting],
        installation_id: str,
    ) -> None:
        if not replacements:
            self._refuse_unmapped_series_changes(uow, declarations, base, installation_id)
            return
        old_ids = [item.old_local_setting_id for item in replacements]
        new_ids = [item.new_local_setting_id for item in replacements]
        if len(set(old_ids)) != len(old_ids) or len(set(new_ids)) != len(new_ids):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a setting may be replaced at most once",
            )
        if set(old_ids) & set(new_ids):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "replacement chains are not allowed",
            )
        declared = {item.setting_ref.key: item for item in declarations}
        base_by_key = {item.setting_ref.key: item for item in base}
        for replacement in replacements:
            old_definition = self._local_definition(uow, replacement.old_local_setting_id)
            new_definition = self._local_definition(uow, replacement.new_local_setting_id)
            if (
                old_definition["installation_id"] != installation_id
                or new_definition["installation_id"] != installation_id
            ):
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "both definitions must belong to this installation",
                )
            if old_definition["series_id"] != new_definition["series_id"]:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "a replacement must stay inside one local setting series",
                )
            successor = uow.query(
                "SELECT child_id FROM local_setting_successors WHERE parent_id=?",
                (replacement.old_local_setting_id,),
            )
            if not successor or successor[0]["child_id"] != replacement.new_local_setting_id:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the new definition is not the strict successor of the old one",
                )
            heads = {head.local_setting_id: head for head in uow.list_local_setting_heads(installation_id)}
            if replacement.new_local_setting_id not in heads:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the new definition is no longer the current head",
                )
            old_key = f"local:{replacement.old_local_setting_id}"
            new_key = f"local:{replacement.new_local_setting_id}"
            if new_key in base_by_key:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the new definition already appears in the current configuration",
                )
            entries = [item for key, item in base_by_key.items() if key == old_key]
            if len(entries) != 1:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the replaced definition does not appear exactly once in the current configuration",
                )
            current_value = entries[0]
            if current_value.value.value_state is not ValueState.known:
                raise ContractError(
                    ErrorCode.PRECONDITION_UNVERIFIED,
                    "the current value of the replaced setting is unknown; confirm it first",
                )
            if not _same_scalar(
                current_value.value.value, replacement.expected_old_value
            ) or current_value.unit != replacement.expected_old_unit:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the declared old value or unit does not match the current configuration",
                )
            declaration = declared.get(new_key)
            if declaration is None or declaration.value is None:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the replacement needs one explicit declaration of the new value",
                )
            if old_key in declared:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the replaced definition must not be declared again in the same adoption",
                )
        self._refuse_unmapped_series_changes(
            uow, declarations, base, installation_id, replaced=set(old_ids)
        )

    def _refuse_series_duplicates(
        self, uow: UnitOfWork, settings: Sequence[ConfirmedSetting]
    ) -> None:
        """One configuration holds one executable reference per local series.

        The rule applies to every origin: a baseline, a recommendation, a
        ChangeSet merge and a rollback snapshot each end in one executable
        value per local setting series, not two competing definitions.
        """
        seen: dict[str, str] = {}
        for setting in settings:
            if setting.setting_ref.kind != "local":
                continue
            definition = self._local_definition(uow, setting.setting_ref.local_setting_id)
            series_id = definition["series_id"]
            previous = seen.get(series_id)
            if previous is not None and previous != setting.setting_ref.local_setting_id:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "a configuration holds one executable value per local setting series",
                    field_errors=[FieldError(
                        path="declared_settings", code="conflict",
                        message="two definitions of one series in the same configuration",
                    )],
                )
            seen[series_id] = setting.setting_ref.local_setting_id

    def _refuse_unmapped_series_changes(
        self,
        uow: UnitOfWork,
        declarations: Sequence[SettingDeclaration],
        base: Sequence[ConfirmedSetting],
        installation_id: str,
        *,
        replaced: set[str] | None = None,
    ) -> None:
        replaced = replaced or set()
        base_series: dict[str, str] = {}
        for setting in base:
            if setting.setting_ref.kind != "local":
                continue
            if setting.setting_ref.local_setting_id in replaced:
                continue
            definition = self._local_definition(uow, setting.setting_ref.local_setting_id)
            base_series[setting.setting_ref.local_setting_id] = definition["series_id"]
        for declaration in declarations:
            if declaration.setting_ref.kind != "local":
                continue
            new_id = declaration.setting_ref.local_setting_id
            definition = self._local_definition(uow, new_id)
            if definition["installation_id"] != installation_id:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the declared definition belongs to another installation",
                )
            for old_id, series_id in base_series.items():
                if series_id == definition["series_id"] and old_id != new_id:
                    raise ContractError(
                        ErrorCode.LOCAL_SETTING_CONFLICT,
                        "a same-series definition change requires an explicit replacement mapping",
                    )

    @staticmethod
    def _verify_changes(
        settings: Sequence[ConfirmedSetting], change_set: Mapping[str, Any]
    ) -> None:
        by_key = {setting.setting_ref.key: setting for setting in settings}
        for change in change_set.get("changes") or []:
            ref = SettingRef.model_validate(ref_shape(change.get("setting_ref")))
            entry = by_key.get(ref.key)
            expected = change.get("new_value")
            if (
                entry is None
                or entry.value.value_state is not ValueState.known
                or entry.value.value != expected
            ):
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    "the adopted configuration does not reflect one of the ChangeSet changes",
                    field_errors=[FieldError(
                        path="declared_settings", code="conflict",
                        message="a ChangeSet change is missing or differs",
                    )],
                )

    # -- objects ------------------------------------------------------------
    def _write_references(
        self, uow: UnitOfWork, applied: AppliedConfiguration, declaration: PlayerDeclaration
    ) -> None:
        owner = applied.id
        uow.insert_reference(owner, "declaration", ObjectRef(
            entity_type="PlayerDeclaration", entity_id=declaration.id,
        ))
        uow.insert_reference(owner, "installation", ObjectRef(
            entity_type="GameInstallation", entity_id=applied.installation_id,
        ))
        uow.insert_reference(owner, "game_context", ObjectRef(
            entity_type="GameContext", entity_id=applied.game_context_id,
        ))
        uow.insert_reference(owner, "device_snapshot", ObjectRef(
            entity_type="DeviceSnapshot", entity_id=applied.device_snapshot_id,
        ))
        if applied.parent_applied_id is not None:
            uow.insert_reference(owner, "parent", ObjectRef(
                entity_type="AppliedConfiguration", entity_id=applied.parent_applied_id,
            ))
        if applied.change_set_id is not None:
            uow.insert_reference(owner, "change_set", ObjectRef(
                entity_type="ChangeSet", entity_id=applied.change_set_id,
            ))
        if applied.restored_from_applied_id is not None:
            uow.insert_reference(owner, "restored_from", ObjectRef(
                entity_type="AppliedConfiguration",
                entity_id=applied.restored_from_applied_id,
            ))
        if applied.recommendation_id is not None:
            uow.insert_reference(owner, "recommendation", ObjectRef(
                entity_type="RecommendationVersion", entity_id=applied.recommendation_id,
            ))

    # -- lookups ------------------------------------------------------------
    def _has_registered(self, context: MutationContext, route: str) -> bool:
        return self.repository.find_idempotency(
            context.user_scope_id, "POST", route, context.idempotency_key
        ) is not None

    def _installation(
        self, uow: UnitOfWork, installation_id: str, user_scope_id: str
    ) -> dict[str, Any]:
        stored = uow.latest_for_series("GameInstallation", installation_id)
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the installation is not available"
            )
        payload = stored.payload
        if payload.get("user_scope_id") != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the installation is not available"
            )
        return {
            "series_id": stored.series_id,
            "game_id": payload.get("game_id"),
            "revision": payload.get("revision"),
        }

    def _context(
        self, uow: UnitOfWork, context_id: str, installation: Mapping[str, Any]
    ) -> StoredObject:
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
                ErrorCode.REFERENCE_INVALID,
                "the game context belongs to another installation",
            )
        if payload.get("game_id") != installation["game_id"]:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID, "the game context belongs to another game"
            )
        return stored

    def _device_snapshot(self, uow: UnitOfWork, snapshot_id: str) -> StoredObject:
        stored = uow.find_object("DeviceSnapshot", snapshot_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced device snapshot does not exist",
                field_errors=[FieldError(
                    path="device_snapshot_id", code="reference", message="unknown snapshot",
                )],
            )
        return stored

    def _load_applied(self, uow: UnitOfWork, applied_id: str | None) -> AppliedConfiguration:
        if applied_id is None:
            raise ContractError(ErrorCode.REFERENCE_INVALID, "the configuration id is missing")
        stored = uow.find_object("AppliedConfiguration", applied_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced applied configuration does not exist",
            )
        return AppliedConfiguration.model_validate_json(stored.payload_bytes)

    def _recommendation(self, uow: UnitOfWork, recommendation_id: str | None) -> None:
        if recommendation_id is None or uow.find_object(
            "RecommendationVersion", recommendation_id
        ) is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced recommendation does not exist",
                field_errors=[FieldError(
                    path="recommendation_id", code="reference", message="unknown recommendation",
                )],
            )

    def _change_set(
        self, uow: UnitOfWork, request: AdoptConfigurationInput
    ) -> tuple[dict[str, Any], str]:
        stored = uow.find_object("ChangeSet", request.change_set_id or "")
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced ChangeSet does not exist",
                field_errors=[FieldError(
                    path="change_set_id", code="reference", message="unknown ChangeSet",
                )],
            )
        change_set = stored.payload
        base_id = change_set.get("base_applied_id")
        if request.base_applied_id != base_id:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "base_applied_id must equal the ChangeSet base",
                field_errors=[FieldError(
                    path="base_applied_id", code="conflict",
                    message="differs from ChangeSet.base_applied_id",
                )],
            )
        return change_set, base_id

    def _setting_ref(
        self, uow: UnitOfWork, ref: SettingRef, installation_id: str
    ) -> None:
        if ref.kind == "local":
            definition = self._local_definition(uow, ref.local_setting_id)
            if definition.get("installation_id") != installation_id:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the referenced definition belongs to another installation",
                    field_errors=[FieldError(
                        path="setting_ref", code="reference",
                        message="cross-installation definition",
                    )],
                )
            return
        record = uow.find_object("ImportedRecord", ref.record.record_snapshot_id)
        if record is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced imported record does not exist",
                field_errors=[FieldError(
                    path="setting_ref.record", code="reference", message="unknown record",
                )],
            )
        stored = ImportedRecord.model_validate_json(record.payload_bytes)
        # The reference must match the record it names on every locating field:
        # a hash alone does not prove which capture, scope or type it came from.
        mismatched = [
            name for name, expected, actual in (
                ("source_revision_id", ref.record.source_revision_id, stored.source_revision_id),
                ("capture_scope_id", ref.record.capture_scope_id, stored.capture_scope_id),
                ("local_record_id", ref.record.local_record_id, stored.local_record_id),
                ("record_type", ref.record.record_type, stored.record_type),
                ("payload_hash", ref.record.payload_hash, stored.payload_hash),
            )
            if expected != actual
        ]
        if mismatched:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the knowledge reference does not match the imported record: "
                + ", ".join(mismatched),
                field_errors=[
                    FieldError(path=f"setting_ref.record.{name}", code="reference",
                               message="differs from the stored record")
                    for name in mismatched
                ],
            )
        if stored.record_type != "settings":
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "only an imported setting record can be used as a knowledge setting reference",
                field_errors=[FieldError(
                    path="setting_ref.record.record_type", code="enum",
                    message=f"record type is {stored.record_type}",
                )],
            )
        usable = uow.query(
            "SELECT usable FROM imported_record_keys WHERE record_snapshot_id=?",
            (record.object_id,),
        )
        if not usable or not usable[0]["usable"]:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "a diagnostic import record is not consumable as a setting reference",
            )

    def _local_definition(self, uow: UnitOfWork, definition_id: str | None) -> dict[str, Any]:
        stored = uow.find_object("LocalSettingDefinition", definition_id or "")
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced local setting definition does not exist",
                field_errors=[FieldError(
                    path="setting_ref", code="reference", message="unknown definition",
                )],
            )
        return stored.payload

    @staticmethod
    def _declaration_evidence(managed: Any) -> EvidenceRef:
        return EvidenceRef(
            kind="artifact",
            file_id=managed.id,
            content_hash=managed.manifest.sha256,
            locator=managed.manifest.relative_name,
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
                        "the historical evidence file does not exist",
                    )
                if stored.payload.get("file_status") != "published":
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the historical evidence file is not published",
                    )
                if stored.payload.get("manifest", {}).get("sha256") != ref.content_hash:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the historical evidence hash does not match the stored file",
                        field_errors=[FieldError(
                            path="evidence_refs", code="reference",
                            message="artifact content_hash does not match the managed file",
                        )],
                    )
            elif ref.kind == "source_location":
                if uow.find_object("SourceRevision", ref.source_revision_id) is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the historical evidence source revision does not exist",
                    )
            elif ref.kind == "local":
                if uow.find_object(ref.entity_type, ref.entity_id) is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the historical evidence record does not exist",
                    )
            else:
                if uow.find_object("ImportedRecord", ref.record.record_snapshot_id) is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the historical knowledge evidence does not exist",
                    )

    def _segment_check(
        self, uow: UnitOfWork, request: HistoricalConfigurationInput
    ) -> CheckResult:
        stored = uow.find_object("SessionSegment", request.segment_ref.segment_id)
        if stored is None:
            # PERF-T02 owns session storage; until it lands the segment cannot be
            # verified, and that limit is recorded instead of assumed.
            return _check(
                "applied.segment",
                CheckStatus.not_run,
                "session segments are not stored yet (PERF-T02); the segment reference"
                " could not be verified",
            )
        payload = stored.payload
        if payload.get("lifecycle") != "ended":
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "a historical configuration needs an ended segment",
            )
        # SessionSegment is an aggregate: its contract field is `revision`.
        revision = payload.get("revision", payload.get("version"))
        if revision != request.segment_ref.segment_revision:
            raise ContractError(
                ErrorCode.BINDING_REVISION_CONFLICT,
                "the segment changed; re-read it before registering a historical configuration",
            )
        return _check(
            "applied.segment",
            CheckStatus.pass_,
            "the segment revision matches and the segment is ended;"
            " binding revision and period containment stay with PERF-T02",
        )


# -- pure helpers -------------------------------------------------------------


def ref_shape(value: Any) -> Any:
    """A ChangeSet may store its setting ref as JSON; validate it as one."""
    if isinstance(value, SettingRef):
        return value.model_dump(mode="json")
    return value


def _same_scalar(current: Any, expected: Any) -> bool:
    """Scalar equality that keeps int/float/bool apart, as the contract does."""
    if type(current) is not type(expected):
        return False
    return current == expected


def _overlay(
    base: Sequence[ConfirmedSetting], additions: Sequence[ConfirmedSetting]
) -> list[ConfirmedSetting]:
    merged = {setting.setting_ref.key: setting for setting in base}
    for setting in additions:
        merged[setting.setting_ref.key] = setting
    return list(merged.values())


def _confirm(
    declaration: SettingDeclaration, evidence: EvidenceRef, now: str
) -> ConfirmedSetting:
    method = (
        AcquisitionMethod.player_confirmed
        if declaration.claimed_activation is ActivationState.active_confirmed
        else AcquisitionMethod.user_provided
    )
    if declaration.value is None:
        value = FieldValue[Any](
            value_state=ValueState.unknown,
            value=None,
            unknown_reason=declaration.unknown_reason,
            acquisition_status=AcquisitionStatus.not_attempted,
        )
    else:
        value = FieldValue[Any](
            value_state=ValueState.known,
            value=declaration.value,
            acquisition_status=AcquisitionStatus.succeeded,
            provenance=[Provenance(
                method=method,
                observed_at=None,
                recorded_at=now,
                evidence_refs=[evidence],
                note="player declaration",
            )],
        )
    return ConfirmedSetting(
        setting_ref=declaration.setting_ref,
        setting_identity_ref=None,
        value=value,
        unit=declaration.unit,
        confirmation_method=method,
        activation_state=declaration.claimed_activation,
    )


def _reduce_activation(
    settings: Sequence[ConfirmedSetting], claimed: ActivationState
) -> ActivationState:
    """State §2 reduction over the declared settings of this configuration."""
    states = [setting.activation_state for setting in settings]
    if not states:
        return claimed
    if ActivationState.conflicting in states:
        return ActivationState.conflicting
    if ActivationState.restart_pending in states:
        return ActivationState.restart_pending
    if ActivationState.unknown in states:
        return ActivationState.unknown
    if all(state is ActivationState.not_applicable for state in states):
        return ActivationState.not_applicable
    if all(state is ActivationState.active_confirmed for state in states):
        return ActivationState.active_confirmed
    return ActivationState.unknown


__all__ = [
    "ADOPTION_ROUTE",
    "ADOPTION_RULE_VERSION",
    "HISTORICAL_ROUTE",
    "AdoptionService",
]
