"""Public game identity, declaration slots and purpose-level reuse (02).

Only local data is read: the catalog never goes online, probes hardware or calls
a model.  Identity is written as provisional until evidence closes it; two
series are never merged on a name alone.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence
from uuid import UUID, uuid5

from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    FieldValue,
    KnowledgeRecordRef,
    MutationContext,
    Provenance,
    ResourceResult,
)
from game_app.contracts.device import AdapterCapabilities, GameContext
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    CollectionResult,
    CollectionStopReason,
    CoverageStatus,
    GameIdentityStatus,
    GapPriority,
    GapStatus,
    PublicationStatus,
    TaskState,
    TaskType,
    ValueState,
)
from game_app.contracts.knowledge import (
    is_registered_scope,
    CoverageCell,
    Game,
    GameCatalogQuery,
    GameChoice,
    GameChoicePage,
    GameSelection,
    GameSelectionRequest,
    IdentityMapping,
    IdentityResolution,
    ImportIdentityMappingRequest,
    ImportedRecord,
    KnowledgeGap,
    KnowledgePackage,
    ReuseDecision,
)
from game_app.contracts.primitives import ObjectRef, new_id, utc_now
from game_app.contracts.tasks import DomainResult, KnowledgeInput
from game_app.errors import ContractError, ErrorCode, FieldError
from game_app.collection import (
    COLLECTION_RULE_VERSION,
    CollectedGap,
    CollectedSource,
    CollectionProtocolError,
    CollectionSnapshotWriter,
    FollowupPlanner,
    GapSpec,
    canonical_url,
    parse_extraction_reply,
    parse_search_reply,
    source_revision_id,
    source_scope_id,
)
from game_app.files import FileService, PublishHooks
from game_app.importer import ImportPublisher, record_payload_references
from game_app.repository import ProductRepository, StoredObject, UnitOfWork, canonical_json
from game_app.tasks import BudgetExhausted, HandlerOutcome, TaskContext, TaskStopReason, UnknownCall

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
SELECTION_ROUTE = "/api/game-selections"
IMPORT_IDENTITY_ROUTE = "/api/import-identity-mappings"
SELECTION_RULE_VERSION = "game-selection/0.4"
IDENTITY_RULE_VERSION = "identity-mapping/0.4"
_SELECTION_FILE = "selection/request.json"


def _unknown_check(check_id: str, method: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version="checks/0.4",
        status=CheckStatus.not_run,
        input_refs=[],
        evidence_refs=[],
        checked_at=None,
        method=method,
    )


def _norm(value: str) -> str:
    """Display/search normalisation: collapses internal whitespace."""
    return " ".join(value.split()).casefold()


def _exact(value: str) -> str:
    """Exact-name normalisation: NFC, trim, casefold — nothing else.

    Automatic identity matching must not treat a differently spaced name as the
    same declaration; the declaration slot uses the same rule.
    """
    return unicodedata.normalize("NFC", value).strip().casefold()


class IdentityService:
    def __init__(
        self,
        repository: ProductRepository,
        files: FileService,
        *,
        context_builder: Callable[
            [ObjectRef, GameSelectionRequest, StoredObject, Any], GameContext
        ] | None = None,
    ) -> None:
        self.repository = repository
        self.files = files
        self._context_builder = context_builder or self._default_context

    @property
    def provider_sends(self) -> int:
        """Identity work never calls a provider; kept explicit for evidence."""
        return 0

    # -- catalog ------------------------------------------------------------
    def list_games(
        self, query: GameCatalogQuery, *, user_scope_id: str | None = None
    ) -> GameChoicePage:
        with self.repository.unit_of_work() as uow:
            games = [
                Game.model_validate_json(stored.payload_bytes)
                for stored in uow.latest_objects("Game")
            ]
        visible = [game for game in games if self._visible(game, user_scope_id)]
        needle = _norm(query.query)
        if needle:
            visible = [game for game in visible if self._matches_text(game, needle)]
        visible.sort(key=lambda game: (game.canonical_name.casefold(), game.id))
        start = 0
        if query.cursor:
            ids = [game.id for game in visible]
            if query.cursor not in ids:
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    "the catalog cursor is no longer valid; re-read the catalog",
                    field_errors=[FieldError(path="cursor", code="reference", message="stale cursor")],
                )
            start = ids.index(query.cursor) + 1
        window = visible[start : start + query.limit]
        next_cursor = window[-1].id if start + query.limit < len(visible) else None
        return GameChoicePage(
            choices=[
                GameChoice(
                    game_id=game.series_id,
                    game_revision_id=game.id,
                    canonical_name=game.canonical_name,
                    platform_scope=list(game.platform_scope),
                    identity_status=game.identity_status,
                )
                for game in window
            ],
            next_cursor=next_cursor,
        )

    def get_selection(self, selection_id: str) -> GameSelection:
        stored = self.repository.get_object("GameSelection", selection_id)
        return GameSelection.model_validate_json(stored.payload_bytes)

    # -- selection ----------------------------------------------------------
    def select_game(
        self,
        request: GameSelectionRequest,
        *,
        user_scope_id: str,
        idempotency_key: str,
        request_id: str,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": "POST",
            "route": SELECTION_ROUTE,
            "request": request.model_dump(mode="json"),
        })).hexdigest()
        manifest = {
            "kind": "game_selection",
            "rule_version": SELECTION_RULE_VERSION,
            "request": request.model_dump(mode="json"),
        }
        published = self.files.publish(
            kind="import",
            files={_SELECTION_FILE: canonical_json(manifest)},
            user_scope_id=user_scope_id,
            method="POST",
            canonical_route=SELECTION_ROUTE,
            key=idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_selection(
                uow, request, user_scope_id, managed
            ),
            hooks=hooks,
        )
        return ResourceResult(
            refs=[ObjectRef.model_validate(ref) for ref in published.refs]
        )

    def _write_selection(
        self,
        uow: UnitOfWork,
        request: GameSelectionRequest,
        user_scope_id: str,
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        declaration_file_id = managed[0].id
        decision = self._decide(uow, request, user_scope_id)

        if decision["state"] == "needs_choice":
            selection = self._selection_record(
                request, declaration_file_id,
                state="needs_choice",
                game_revision_id=None,
                game_context_id=None,
                candidates=decision["candidates"],
            )
            uow.insert_object("GameSelection", selection)
            return [ObjectRef(entity_type="GameSelection", entity_id=selection.id)]

        game_ref = self._ensure_game(uow, decision, user_scope_id)
        stored_game = uow.get_object("Game", game_ref.entity_id)
        context = self._context_builder(game_ref, request, stored_game, managed[0])
        uow.insert_versioned("GameContext", context)
        selection = self._selection_record(
            request, declaration_file_id,
            state="selected",
            game_revision_id=game_ref.entity_id,
            game_context_id=context.id,
            candidates=[],
        )
        uow.insert_object("GameSelection", selection)
        uow.insert_reference(selection.id, "game", game_ref)
        uow.insert_reference(
            selection.id, "game_context", ObjectRef(entity_type="GameContext", entity_id=context.id)
        )
        return [
            ObjectRef(entity_type="GameSelection", entity_id=selection.id),
            game_ref,
            ObjectRef(entity_type="GameContext", entity_id=context.id),
        ]

    def _selection_record(
        self,
        request: GameSelectionRequest,
        declaration_file_id: str,
        *,
        state: str,
        game_revision_id: str | None,
        game_context_id: str | None,
        candidates: Sequence[str],
    ) -> GameSelection:
        return GameSelection(
            id=new_id(),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=utc_now(),
            created_by_task_id=None,
            input=request,
            state=state,
            game_revision_id=game_revision_id,
            game_context_id=game_context_id,
            candidate_revision_ids=list(candidates),
            declaration_file_id=declaration_file_id,
        )

    def _decide(
        self, uow: UnitOfWork, request: GameSelectionRequest, user_scope_id: str
    ) -> dict[str, Any]:
        if request.selected_game_revision_id is not None:
            stored = uow.find_object("Game", request.selected_game_revision_id)
            if stored is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the selected game revision does not exist",
                    field_errors=[FieldError(
                        path="selected_game_revision_id", code="reference", message="unknown revision",
                    )],
                )
            game = Game.model_validate_json(stored.payload_bytes)
            if not self._visible(game, user_scope_id):
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "the selected game revision is not available",
                )
            self._check_installation(uow, request, game, user_scope_id)
            return {"state": "selected", "game": game, "series_id": game.series_id}

        matches = [
            game
            for game in (
                Game.model_validate_json(stored.payload_bytes)
                for stored in uow.latest_objects("Game")
            )
            if game.identity_status is GameIdentityStatus.verified
            and self._matches(game, request)
        ]
        if len(matches) >= 2:
            matches.sort(key=lambda game: (game.canonical_name.casefold(), game.id))
            return {"state": "needs_choice", "candidates": [game.id for game in matches]}
        if len(matches) == 1:
            self._check_installation(uow, request, matches[0], user_scope_id)
            return {"state": "selected", "game": matches[0], "series_id": matches[0].series_id}

        self._check_installation(uow, request, None, user_scope_id)
        return {
            "state": "provisional",
            "raw_name": request.raw_name,
            "platform": request.platform,
        }

    def _ensure_game(
        self, uow: UnitOfWork, decision: dict[str, Any], user_scope_id: str
    ) -> ObjectRef:
        if decision["state"] == "selected":
            return ObjectRef(entity_type="Game", entity_id=decision["game"].id)

        series_id = uow.get_or_create_game_declaration_slot(
            user_scope_id, decision["raw_name"], decision["platform"], new_id()
        )
        existing = uow.latest_for_series("Game", series_id)
        if existing is not None:
            return ObjectRef(entity_type="Game", entity_id=existing.object_id)

        platform = decision["platform"]
        game = Game(
            id=new_id(),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=utc_now(),
            created_by_task_id=None,
            series_id=series_id,
            version=1,
            canonical_name=decision["raw_name"],
            aliases=[],
            platform_scope=[] if platform is None else [platform],
            identity_status=GameIdentityStatus.provisional,
            identity_checks=[_unknown_check(
                "identity.evidence",
                "no verified identity or platform evidence was supplied",
            )],
            declaring_user_scope_id=user_scope_id,
        )
        uow.insert_versioned("Game", game)
        return ObjectRef(entity_type="Game", entity_id=game.id)

    def _check_installation(
        self,
        uow: UnitOfWork,
        request: GameSelectionRequest,
        game: Game | None,
        user_scope_id: str,
    ) -> None:
        if request.installation_id is None:
            return
        stored = uow.latest_for_series("GameInstallation", request.installation_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced installation does not exist",
                field_errors=[FieldError(
                    path="installation_id", code="reference", message="unknown installation",
                )],
            )
        payload = stored.payload
        if payload.get("user_scope_id") != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the referenced installation is not available"
            )
        if game is not None and payload.get("game_id") != game.series_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the installation belongs to another game",
                field_errors=[FieldError(
                    path="installation_id", code="reference", message="cross-game installation",
                )],
            )
        if payload.get("revision") != request.expected_installation_revision:
            raise ContractError(
                ErrorCode.INSTALLATION_REVISION_CONFLICT,
                "the installation changed; re-read it before selecting",
            )

    # -- context ------------------------------------------------------------
    def _default_context(
        self,
        game_ref: ObjectRef,
        request: GameSelectionRequest,
        stored_game: StoredObject,
        declaration: Any,
    ) -> GameContext:
        """Minimal 01 context: declared platform only, everything else unknown.

        DEV-T02 extends this with installations and adapters; it never invents a
        build id, process, adapter capability or device default.
        """
        game = Game.model_validate_json(stored_game.payload_bytes)
        if request.platform is None:
            platform = FieldValue[str](
                value_state=ValueState.unknown,
                value=None,
                unknown_reason="not_provided",
                acquisition_status=AcquisitionStatus.not_attempted,
            )
        else:
            platform = FieldValue[str](
                value_state=ValueState.known,
                value=request.platform,
                acquisition_status=AcquisitionStatus.succeeded,
                provenance=[Provenance(
                    method=AcquisitionMethod.user_provided,
                    observed_at=None,
                    recorded_at=utc_now(),
                    evidence_refs=[EvidenceRef(
                        kind="artifact",
                        file_id=declaration.id,
                        content_hash=declaration.manifest.sha256,
                        locator=_SELECTION_FILE,
                    )],
                    note="player-declared platform from the selection request",
                )],
            )
        return GameContext(
            id=new_id(),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=utc_now(),
            created_by_task_id=None,
            series_id=new_id(),
            version=1,
            game_id=game.series_id,
            game_revision_id=game.id,
            installation_id=request.installation_id,
            platform=platform,
            build_identifiers=[],
            process_candidates=[],
            selected_process=None,
            adapter_id=None,
            adapter_version=None,
            capabilities=AdapterCapabilities(
                can_identify=_unknown_check("adapter.identify", "no verified adapter"),
                can_read_config=_unknown_check("adapter.read_config", "no verified adapter"),
                can_bind_capture=_unknown_check("adapter.bind_capture", "no verified adapter"),
            ),
            checks=[_unknown_check("context.build", "build identifiers not obtained")],
        )

    # -- identity resolution -------------------------------------------------
    def resolve_game_identity(
        self,
        *,
        series_id: str,
        platform_scope: Sequence[str],
        evidence_refs: Sequence[EvidenceRef],
        checks: Sequence[CheckResult],
        legacy_namespace: str | None = None,
        legacy_value: str | None = None,
    ) -> IdentityResolution:
        passing = [check for check in checks if check.status is CheckStatus.pass_]
        if not evidence_refs or not passing or not platform_scope:
            return IdentityResolution(
                game_ref=None,
                checks=list(checks),
                reason="identity evidence is insufficient; the series stays provisional",
            )
        if (legacy_namespace is None) != (legacy_value is None):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a legacy identity mapping needs both a namespace and a value",
                field_errors=[FieldError(
                    path="legacy_namespace", code="required", message="namespace and value pair",
                )],
            )
        if legacy_namespace is not None and (
            not legacy_namespace.strip() or not legacy_value.strip()
        ):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a legacy identity mapping needs a non-empty namespace and value",
                field_errors=[FieldError(
                    path="legacy_namespace", code="required", message="blank mapping identity",
                )],
            )
        with self.repository.unit_of_work() as uow:
            problems = self._unresolved_evidence(uow, evidence_refs)
            if problems:
                # Evidence that cannot be located is not evidence: keep the
                # series provisional and report exactly what is missing.
                return IdentityResolution(
                    game_ref=None,
                    checks=list(checks),
                    reason="; ".join(problems),
                )
            latest = uow.latest_for_series("Game", series_id)
            if latest is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID, "the game series does not exist"
                )
            current = Game.model_validate_json(latest.payload_bytes)
            verified = current.model_copy(update={
                "id": new_id(),
                "created_at": utc_now(),
                "version": current.version + 1,
                "platform_scope": list(platform_scope),
                "identity_status": GameIdentityStatus.verified,
                "identity_checks": list(checks),
                "declaring_user_scope_id": None,
            })
            uow.insert_versioned("Game", verified)
            if legacy_namespace is not None:
                uow.insert_object("IdentityMapping", IdentityMapping(
                    id=new_id(),
                    schema_version=PRODUCT_CONTRACT_VERSION,
                    created_at=utc_now(),
                    legacy_namespace=legacy_namespace,
                    legacy_value=legacy_value,
                    target_game_id=verified.series_id,
                    declared_by_user_scope_id=None,
                    checks=list(checks),
                ))
        return IdentityResolution(
            game_ref=ObjectRef(entity_type="Game", entity_id=verified.id),
            checks=list(checks),
            reason="identity verified with located evidence",
        )

    # -- player-declared import identity (2D-C) ------------------------------
    def declare_import_identity(
        self,
        request: Any,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
    ) -> ResourceResult:
        """Let a player name the game one collector run belongs to.

        The declaration is a scoped, auditable mapping: it names the declarer,
        carries a pass check that says it is not external verification, and is
        usable only by that player's imports.
        """
        if isinstance(request, ImportIdentityMappingRequest):
            typed = request
        else:
            typed = ImportIdentityMappingRequest.model_validate_json(
                canonical_json(request)
            )
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": "POST",
            "route": IMPORT_IDENTITY_ROUTE,
            "request": typed.model_dump(mode="json"),
        })).hexdigest()
        with self.repository.unit_of_work() as uow:
            existing = uow.find_idempotency(
                context.user_scope_id,
                "POST",
                IMPORT_IDENTITY_ROUTE,
                context.idempotency_key,
            )
            if existing is not None:
                if existing.lifecycle.value == "tombstoned":
                    raise ContractError(
                        ErrorCode.RESOURCE_GONE,
                        "this declaration was cleaned up; start a new submission",
                    )
                if (
                    existing.request_schema_version != request_schema_version
                    or existing.request_hash != request_hash
                ):
                    raise ContractError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "the same key was already used with different declaration content",
                        field_errors=[FieldError(
                            path="idempotency_key", code="conflict",
                            message="different request hash",
                        )],
                    )
                return ResourceResult.model_validate(json.loads(existing.response_body))

            game = self._declarable_game(
                uow, typed.game_revision_id, context.user_scope_id
            )
            namespace = f"collector_run:{typed.run_id}"
            existing_mappings = [
                IdentityMapping.model_validate_json(stored.payload_bytes)
                for stored in uow.iter_objects("IdentityMapping")
                if stored.payload.get("legacy_namespace") == namespace
                and stored.payload.get("legacy_value") == typed.legacy_game_id
                # Only public mappings and the caller's own declarations are
                # visible here; another player's declaration is not reused and
                # its existence is not leaked.
                and stored.payload.get("declared_by_user_scope_id")
                in (None, context.user_scope_id)
            ]
            if existing_mappings:
                if {item.target_game_id for item in existing_mappings} != {game.series_id}:
                    # Never overwrite: the run's identity is a fact, and the
                    # player must start a new run instead.
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        "this collector run already names another game;"
                        " the existing mapping is not overwritten",
                        field_errors=[FieldError(
                            path="legacy_game_id", code="conflict",
                            message="mapping already points at another game",
                        )],
                    )
                mapping = existing_mappings[0]
            else:
                mapping = IdentityMapping(
                    id=new_id(),
                    schema_version=PRODUCT_CONTRACT_VERSION,
                    created_at=utc_now(),
                    legacy_namespace=namespace,
                    legacy_value=typed.legacy_game_id,
                    target_game_id=game.series_id,
                    declared_by_user_scope_id=context.user_scope_id,
                    checks=[CheckResult(
                        check_id="identity.declared",
                        definition_version=IDENTITY_RULE_VERSION,
                        status=CheckStatus.pass_,
                        input_refs=[],
                        evidence_refs=[],
                        checked_at=utc_now(),
                        method="player declaration",
                        note="the player specified this game for the import;"
                        " this is not an externally verified identity",
                    )],
                )
                uow.insert_object("IdentityMapping", mapping)
            result = ResourceResult(refs=[
                ObjectRef(entity_type="IdentityMapping", entity_id=mapping.id)
            ])
            uow.insert_idempotency(
                user_scope_id=context.user_scope_id,
                method="POST",
                canonical_route=IMPORT_IDENTITY_ROUTE,
                key=context.idempotency_key,
                request_schema_version=request_schema_version,
                request_hash=request_hash,
                response_status=201,
                response_body=canonical_json(result.model_dump(mode="json")),
                result_refs=[ref.model_dump(mode="json") for ref in result.refs],
            )
            return result

    def _declarable_game(
        self, uow: UnitOfWork, game_revision_id: str, user_scope_id: str
    ) -> Game:
        stored = uow.find_object("Game", game_revision_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the game revision does not exist",
                field_errors=[FieldError(
                    path="game_revision_id", code="reference",
                    message="unknown game revision",
                )],
            )
        game = Game.model_validate_json(stored.payload_bytes)
        if game.identity_status is GameIdentityStatus.legacy_unverified:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "a legacy unverified read view cannot be used to declare a new mapping",
                field_errors=[FieldError(
                    path="game_revision_id", code="enum",
                    message="legacy_unverified",
                )],
            )
        if (
            game.identity_status is GameIdentityStatus.provisional
            and game.declaring_user_scope_id != user_scope_id
        ):
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the game revision is not available"
            )
        return game

    def _unresolved_evidence(
        self, uow: UnitOfWork, evidence_refs: Sequence[EvidenceRef]
    ) -> list[str]:
        """Return one message per evidence reference that cannot be located."""
        problems: list[str] = []
        for index, ref in enumerate(evidence_refs):
            try:
                if ref.kind == "artifact":
                    stored = uow.find_object("ManagedFile", ref.file_id)
                    if stored is None:
                        problems.append(f"evidence[{index}]: artifact file does not exist")
                    elif stored.payload.get("file_status") != PublicationStatus.published.value:
                        problems.append(f"evidence[{index}]: artifact file is not published")
                    elif stored.payload.get("manifest", {}).get("sha256") != ref.content_hash:
                        problems.append(f"evidence[{index}]: artifact hash does not match the file")
                elif ref.kind == "source_location":
                    if uow.find_object("SourceRevision", ref.source_revision_id) is None:
                        problems.append(f"evidence[{index}]: source revision does not exist")
                elif ref.kind == "local":
                    if uow.find_object(ref.entity_type, ref.entity_id) is None:
                        problems.append(f"evidence[{index}]: local record does not exist")
                else:
                    if uow.find_object("KnowledgePackage", ref.record.package_id) is None:
                        problems.append(f"evidence[{index}]: knowledge package does not exist")
                    elif uow.find_object("ImportedRecord", ref.record.record_snapshot_id) is None:
                        problems.append(f"evidence[{index}]: imported record does not exist")
            except ContractError:
                problems.append(f"evidence[{index}]: reference points at another entity type")
        return problems

    # -- reuse ---------------------------------------------------------------
    def assess_reuse(
        self,
        *,
        game_context_id: str,
        device_snapshot_id: str | None,
        package_id: str | None,
        operation_scope: str,
    ) -> ReuseDecision:
        if not is_registered_scope(operation_scope):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                f"unregistered operation scope: {operation_scope}",
                field_errors=[FieldError(
                    path="operation_scope", code="enum", message="unregistered scope",
                )],
            )
        context = GameContext.model_validate_json(
            self.repository.get_object("GameContext", game_context_id).payload_bytes
        )
        platform = context.platform.value if context.platform.value_state is ValueState.known else None

        with self.repository.unit_of_work() as uow:
            # Every immutable package version is addressable; an older published
            # version stays consumable after a newer one is prepared.
            packages = [
                KnowledgePackage.model_validate_json(stored.payload_bytes)
                for stored in uow.iter_objects("KnowledgePackage")
            ]
        published = [
            package for package in packages
            if package.publication_status is PublicationStatus.published
            and package.game_id == context.game_id
        ]

        checks: list[CheckResult] = []
        if package_id is not None:
            chosen = next((package for package in packages if package.id == package_id), None)
            if chosen is None or chosen.publication_status is not PublicationStatus.published:
                checks.append(self._reuse_check(
                    "package.available", CheckStatus.fail,
                    "the requested package is missing or not published",
                ))
                return ReuseDecision(
                    package_id=None, mode="discover", checks=checks, gap_keys=[],
                    reason="the requested package cannot be consumed",
                )
        else:
            if platform is None:
                # Unknown platform: keep the newest published package so the
                # decision can report exactly what must be verified.
                compatible = list(published)
            else:
                compatible = [
                    package for package in published if self._platform_ok(package, platform)
                ]
            compatible.sort(key=lambda package: (package.created_at, package.version))
            chosen = compatible[-1] if compatible else None

        if chosen is None:
            checks.append(self._reuse_check(
                "package.available", CheckStatus.fail,
                "no published package covers this game and platform",
            ))
            return ReuseDecision(
                package_id=None, mode="discover", checks=checks, gap_keys=[],
                reason="no reusable package exists for this game and platform",
            )

        if chosen.game_id != context.game_id:
            checks.append(self._reuse_check(
                "package.game", CheckStatus.fail,
                "the package belongs to another game",
            ))
            return ReuseDecision(
                package_id=None, mode="discover", checks=checks, gap_keys=[],
                reason="the requested package was built for another game",
            )
        checks.append(self._reuse_check(
            "package.game", CheckStatus.pass_, "the package belongs to this game",
        ))

        if platform is None:
            # Without a declared platform no package can be called compatible;
            # an unverified relation stays unknown rather than passing.
            checks.append(self._reuse_check(
                "package.platform", CheckStatus.unknown,
                "the context platform is unknown, so package compatibility is unverified",
            ))
            return ReuseDecision(
                package_id=chosen.id, mode="followup", checks=checks,
                gap_keys=["package.platform"],
                reason="declare the platform before reusing another platform's package",
            )

        if not self._platform_ok(chosen, platform):
            checks.append(self._reuse_check(
                "package.platform", CheckStatus.fail,
                f"package platforms {chosen.platform_scope} do not cover {platform}",
            ))
            return ReuseDecision(
                package_id=None, mode="discover", checks=checks, gap_keys=[],
                reason="the package was built for another platform",
            )
        checks.append(self._reuse_check(
            "package.platform", CheckStatus.pass_, "the package platform covers the context",
        ))

        cells = [cell for cell in chosen.coverage_matrix if cell.operation_scope == operation_scope]
        uncovered = [
            cell.key for cell in cells
            if cell.status.value not in ("covered", "not_applicable")
        ]
        open_gaps = [
            f"gap:{gap.id}" for gap in chosen.critical_gaps
            if gap.operation_scope == operation_scope and gap.status.value == "open"
        ]
        gap_keys = sorted(set(uncovered + open_gaps))

        if not cells:
            checks.append(self._reuse_check(
                "package.coverage", CheckStatus.unknown,
                f"the package declares no coverage for {operation_scope}",
            ))
            return ReuseDecision(
                package_id=chosen.id, mode="followup", checks=checks,
                gap_keys=[f"{operation_scope}.coverage"],
                reason="the package has no coverage for this operation scope",
            )
        if gap_keys:
            checks.append(self._reuse_check(
                "package.coverage", CheckStatus.fail,
                f"uncovered cells for {operation_scope}: {', '.join(gap_keys)}",
            ))
            return ReuseDecision(
                package_id=chosen.id, mode="followup", checks=checks, gap_keys=gap_keys,
                reason="the package is reusable but has open gaps for this operation",
            )
        checks.append(self._reuse_check(
            "package.coverage", CheckStatus.pass_,
            f"all declared cells for {operation_scope} are covered",
        ))
        return ReuseDecision(
            package_id=chosen.id, mode="reuse", checks=checks, gap_keys=[],
            reason="the package covers the requested operation scope",
        )

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _visible(game: Game, user_scope_id: str | None) -> bool:
        if game.identity_status is GameIdentityStatus.provisional:
            return user_scope_id is not None and game.declaring_user_scope_id == user_scope_id
        return True

    @staticmethod
    def _matches_text(game: Game, needle: str) -> bool:
        if needle in _norm(game.canonical_name):
            return True
        return any(
            alias.status is CheckStatus.pass_ and needle in _norm(alias.name)
            for alias in game.aliases
        )

    def _matches(self, game: Game, request: GameSelectionRequest) -> bool:
        name = _exact(request.raw_name)
        if not self._platform_ok_game(game, request.platform):
            return False
        if _exact(game.canonical_name) == name:
            return True
        return any(
            alias.status is CheckStatus.pass_
            and _exact(alias.name) == name
            and (alias.platform is None or request.platform is None
                 or _norm(alias.platform) == _norm(request.platform))
            for alias in game.aliases
        )

    @staticmethod
    def _platform_ok_game(game: Game, platform: str | None) -> bool:
        if platform is None:
            return True
        return _norm(platform) in {_norm(item) for item in game.platform_scope}

    @staticmethod
    def _platform_ok(package: KnowledgePackage, platform: str | None) -> bool:
        # An unknown platform never proves compatibility.
        if platform is None:
            return False
        return _norm(platform) in {_norm(item) for item in package.platform_scope}

    @staticmethod
    def _reuse_check(check_id: str, status: CheckStatus, note: str) -> CheckResult:
        return CheckResult(
            check_id=check_id,
            definition_version="reuse-checks/0.4",
            status=status,
            input_refs=[],
            evidence_refs=[],
            checked_at=None if status is CheckStatus.not_run else utc_now(),
            method="deterministic local rule",
            note=note,
        )


# --- KNW-T02: coverage reduction and package publication --------------------

PACKAGE_ROUTE = "/internal/knowledge-packages"
PACKAGE_RULE_VERSION = "coverage/0.4"
# KnowledgeInput carries no declared scope (data §9 fixes that DTO); the task is
# public collection, so its one scope is the registered collection scope.
KNOWLEDGE_TASK_SCOPE = "collection.public_graphics"
IMAGE_DIMENSION = "graphics.image_figures"
_PACKAGE_NS = UUID("5b2c7d19-4e8f-4a61-9c3d-2f7e8a0b1c44")
_DECLARATION_FILE = "declaration.json"

# No image adapter has G-IMAGE evidence; the registry stays empty until one is
# independently verified.  An empty registry is the enablement gate itself.
IMAGE_ADAPTERS_VERIFIED: frozenset[str] = frozenset()

_SCOPE_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "collection.public_graphics": (
        "graphics.catalog",
        "hardware.device_forms",
        "display.capabilities",
        "tradeoffs.directions",
        "measurement.environment",
        "version.dependencies",
    ),
    "recommendation.configuration": (
        "graphics.catalog",
        "tradeoffs.directions",
        "version.dependencies",
    ),
    "recommendation.target": (
        "measurement.environment",
        "display.capabilities",
    ),
    "comparison.settings_effect": (
        "measurement.environment",
        "tradeoffs.directions",
    ),
}
_DIAGNOSIS_DIMENSIONS = ("graphics.catalog", "version.dependencies")

# Which record types close a cell gap, so the planner knows what a gain is.
_CELL_EXPECTS: dict[str, tuple[str, ...]] = {
    "graphics.catalog": ("settings", "configurations"),
    "hardware.device_forms": ("environments",),
    "display.capabilities": ("evidence", "constraints"),
    "tradeoffs.directions": ("configurations",),
    "measurement.environment": ("evidence",),
    "version.dependencies": ("version_impacts", "constraints"),
    "graphics.image_figures": ("evidence",),
}


def package_series_id(game_id: str, declared_scope: str) -> str:
    """One version series per (game, declared operation scope)."""
    return uuid5(_PACKAGE_NS, f"package-series:{game_id}:{declared_scope}").hex


@dataclass
class _CollectionRun:
    """What one collection attempt produced; never a task-state projection."""

    started_at: str = ""
    sources: dict[str, CollectedSource] = field(default_factory=dict)
    open_gaps: dict[str, GapSpec] = field(default_factory=dict)
    used: dict[str, int] = field(default_factory=dict)
    read_urls: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    rounds: int = 0
    gained: int = 0
    stop_reason: CollectionStopReason | None = None
    unknown_call: bool = False
    budget_exhausted: bool = False

    def checks(self) -> list[CheckResult]:
        return [
            _rule_check(
                "collection.rounds",
                CheckStatus.pass_ if self.rounds else CheckStatus.not_run,
                f"{self.rounds} follow-up round(s) ran; {len(self.read_urls)} URL(s)"
                " were read once each",
            ),
            _rule_check(
                "collection.gain",
                CheckStatus.pass_ if self.gained else CheckStatus.unknown,
                f"{self.gained} key field(s) were added; the number of pages is"
                " not information gain",
            ),
            _rule_check(
                "collection.stop",
                CheckStatus.pass_ if self.stop_reason is not None else CheckStatus.unknown,
                "the run stopped with "
                + ("no reason recorded" if self.stop_reason is None
                   else self.stop_reason.value),
            ),
            _rule_check(
                "collection.read_errors",
                CheckStatus.fail if self.errors else CheckStatus.pass_,
                "; ".join(self.errors) if self.errors
                else "no read or extraction error was recorded",
            ),
        ]


def image_adapter_status() -> tuple[bool, CheckResult]:
    """The image gate: disabled until G-IMAGE evidence verifies an adapter."""
    enabled = bool(IMAGE_ADAPTERS_VERIFIED)
    return enabled, CheckResult(
        check_id="image.adapter",
        definition_version=PACKAGE_RULE_VERSION,
        status=CheckStatus.pass_ if enabled else CheckStatus.not_run,
        input_refs=[],
        evidence_refs=[],
        checked_at=utc_now() if enabled else None,
        method="enablement gate",
        note=(
            "an independently verified image adapter is enabled"
            if enabled
            else "no G-IMAGE evidence: the image adapter stays disabled and"
                 " figure, legend and unit conditions are not read"
        ),
    )


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _payload(record: ImportedRecord) -> dict[str, Any]:
    return record.payload if isinstance(record.payload, dict) else {}


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


def _knowledge_evidence(package_id: str, records: Sequence[ImportedRecord]) -> list[EvidenceRef]:
    return [
        EvidenceRef(kind="knowledge", record=_record_ref(package_id, record))
        for record in records
    ]


def _rule_check(check_id: str, status: CheckStatus, note: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=PACKAGE_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else utc_now(),
        method="deterministic coverage rule",
        note=note,
    )


class CoverageEvaluator:
    """Reduce accepted records to per-cell coverage and the gaps they leave.

    The reduction is deliberately literal: a cell is ``covered`` only when the
    key subitems this operation needs have a located, accepted basis.  A check
    that never ran is never ``covered``, and an absent basis is ``unknown`` —
    not ``not_applicable``.
    """

    def dimensions_for(self, declared_scope: str, *, image_required: bool) -> tuple[str, ...]:
        if not is_registered_scope(declared_scope):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                f"unregistered operation scope: {declared_scope}",
                field_errors=[FieldError(
                    path="declared_scope", code="enum", message="unregistered scope",
                )],
            )
        if declared_scope in _SCOPE_DIMENSIONS:
            base = _SCOPE_DIMENSIONS[declared_scope]
        else:
            base = _DIAGNOSIS_DIMENSIONS
        if image_required:
            return (*base, IMAGE_DIMENSION)
        return base

    def evaluate(
        self,
        *,
        package_id: str,
        declared_scope: str,
        records: Sequence[ImportedRecord],
        diagnostics: Sequence[ImportedRecord] = (),
        image_required: bool = False,
    ) -> tuple[list[CoverageCell], list[KnowledgeGap]]:
        by_type: dict[str, list[ImportedRecord]] = {}
        for record in records:
            by_type.setdefault(record.record_type, []).append(record)

        cells: list[CoverageCell] = []
        for key in self.dimensions_for(declared_scope, image_required=image_required):
            status, basis, note = self._reduce(key, by_type, package_id)
            cells.append(CoverageCell(
                key=key,
                dimensions={},
                operation_scope=declared_scope,
                status=status,
                evidence_refs=_knowledge_evidence(package_id, basis),
                note=note,
            ))

        gaps: list[KnowledgeGap] = []
        for cell in cells:
            if cell.status in (CoverageStatus.covered, CoverageStatus.not_applicable):
                continue
            gaps.append(KnowledgeGap(
                id=uuid5(_PACKAGE_NS, f"{package_id}:gap:{cell.key}").hex,
                legacy_gap_id=None,
                operation_scope=declared_scope,
                priority=(
                    GapPriority.P0
                    if cell.status is CoverageStatus.unknown
                    else GapPriority.P1
                ),
                question=f"close coverage cell {cell.key}: {cell.note}",
                record_refs=[
                    ref.record for ref in cell.evidence_refs if ref.record is not None
                ],
                diagnostic_record_ids=[],
                status=GapStatus.open,
                stop_reason=None,
                evidence_refs=[],
                checks=[_rule_check(
                    f"coverage.{cell.key}",
                    CheckStatus.unknown
                    if cell.status is CoverageStatus.unknown
                    else CheckStatus.fail,
                    cell.note,
                )],
            ))
        for record in diagnostics:
            gaps.append(KnowledgeGap(
                id=uuid5(_PACKAGE_NS, f"{package_id}:diagnostic:{record.id}").hex,
                legacy_gap_id=record.local_record_id,
                operation_scope=declared_scope,
                priority=GapPriority.P2,
                question=(
                    f"resolve the isolated {record.record_type} record"
                    f" {record.local_record_id!r}; it is diagnostic evidence only"
                ),
                record_refs=[],
                diagnostic_record_ids=[record.id],
                status=GapStatus.open,
                stop_reason=None,
                evidence_refs=[],
                checks=[_rule_check(
                    "coverage.diagnostic",
                    CheckStatus.fail,
                    "the record was isolated at import and never becomes package evidence",
                )],
            ))
        return cells, gaps

    # -- one cell ----------------------------------------------------------
    def _reduce(
        self,
        key: str,
        by_type: dict[str, list[ImportedRecord]],
        package_id: str,
    ) -> tuple[CoverageStatus, list[ImportedRecord], str]:
        if key == "graphics.catalog":
            entries = by_type.get("settings", [])
            if not entries:
                return (
                    CoverageStatus.unknown, [],
                    "no accepted setting record locates a menu entry",
                )
            nameless = [entry for entry in entries if not _text(_payload(entry).get("raw_name"))]
            if nameless:
                return (
                    CoverageStatus.partially_covered, entries,
                    f"{len(nameless)} accepted setting(s) lost their original name;"
                    " the catalog cannot be matched to a menu",
                )
            return (
                CoverageStatus.covered, entries,
                "every accepted setting keeps its original name; this is the declared"
                " menu scope, not a claim that the whole game menu was read",
            )

        if key == "hardware.device_forms":
            environments = by_type.get("environments", [])
            if not environments:
                return (
                    CoverageStatus.unknown, [],
                    "no accepted environment record states a device condition",
                )
            known = [
                environment for environment in environments
                if self._environment_has_known_field(environment)
            ]
            if not known:
                return (
                    CoverageStatus.partially_covered, environments,
                    "environment records exist but none states a known device condition",
                )
            return (
                CoverageStatus.covered, known,
                "the declared device conditions are located in accepted environment records",
            )

        if key == "display.capabilities":
            exclusions = [
                record for record in by_type.get("constraints", [])
                if _payload(record).get("kind") == "exclusion"
                and _payload(record).get("verified_scope") is True
            ]
            if exclusions:
                return (
                    CoverageStatus.not_applicable, exclusions,
                    "a verified exclusion constraint states the capability is not applicable",
                )
            observations = [
                record for record in by_type.get("evidence", [])
                if _payload(record).get("detected_state") in ("detected", "not_detected")
            ]
            if observations:
                return (
                    CoverageStatus.covered, observations,
                    "accepted evidence records the detected/not-detected state of the capability",
                )
            supports = [
                record for record in by_type.get("constraints", [])
                if _payload(record).get("kind") in ("support", "exclusion")
            ]
            if supports:
                return (
                    CoverageStatus.partially_covered, supports,
                    "only a constraint states support; no observation records the"
                    " detected state for resolution, upscaling, frame generation or ray tracing",
                )
            return (
                CoverageStatus.unknown, [],
                "no accepted evidence records a detected/not-detected state for"
                " resolution, upscaling, frame generation or ray tracing",
            )

        if key == "tradeoffs.directions":
            configurations = by_type.get("configurations", [])
            if not configurations:
                return (
                    CoverageStatus.unknown, [],
                    "no accepted configuration record states a trade-off direction",
                )
            signatures = {
                canonical_json(
                    _payload(record).get("assignments")
                    or _payload(record).get("preset_reference")
                )
                for record in configurations
            }
            if len(signatures) < 2:
                return (
                    CoverageStatus.partially_covered, configurations,
                    "only one usable configuration direction exists; a second one is"
                    " not invented to fill the cell",
                )
            return (
                CoverageStatus.covered, configurations,
                "at least two distinct accepted configurations state different"
                " performance/visual directions",
            )

        if key == "measurement.environment":
            measurements = [
                record for record in by_type.get("evidence", [])
                if _payload(record).get("metric") is not None
            ]
            if not measurements:
                return (
                    CoverageStatus.unknown, [],
                    "no accepted evidence record states a measurement metric",
                )
            complete = [
                record for record in measurements
                if _payload(record).get("numeric") is not None
                and _text(_payload(record).get("unit"))
            ]
            if not complete:
                return (
                    CoverageStatus.partially_covered, measurements,
                    "measurements are archived as stated but state no numeric value;"
                    " fps_unspecified does not close an average-FPS or target gap",
                )
            if len(complete) < len(measurements):
                return (
                    CoverageStatus.partially_covered, complete,
                    f"{len(measurements) - len(complete)} measurement(s) state no"
                    " numeric value and stay archived without closing the cell",
                )
            return (
                CoverageStatus.covered, complete,
                "every accepted measurement states metric, numeric value and unit",
            )

        if key == "version.dependencies":
            impacts = by_type.get("version_impacts", [])
            dependencies = [
                record for record in by_type.get("constraints", [])
                if _payload(record).get("kind") == "dependency"
            ]
            if not impacts and not dependencies:
                return (
                    CoverageStatus.unknown, [],
                    "no accepted version impact or dependency constraint is recorded",
                )
            if impacts and all(_text(_payload(item).get("version_raw")) for item in impacts):
                return (
                    CoverageStatus.covered, impacts,
                    "accepted version impacts name the platform and version they apply to",
                )
            return (
                CoverageStatus.partially_covered, [*impacts, *dependencies],
                "version/dependency statements exist but the version range or relation"
                " is incomplete",
            )

        if key == IMAGE_DIMENSION:
            enabled, _check = image_adapter_status()
            if not enabled:
                return (
                    CoverageStatus.unknown, [],
                    "no G-IMAGE evidence: the image adapter is disabled, so figure,"
                    " legend and unit conditions were not read (not_run)",
                )
            return (
                CoverageStatus.unknown, [],
                "an image adapter is enabled but no image extraction record exists",
            )

        raise ContractError(
            ErrorCode.INTERNAL_ERROR, f"no coverage rule is registered for {key}"
        )

    @staticmethod
    def _environment_has_known_field(record: ImportedRecord) -> bool:
        fields = _payload(record).get("fields")
        if not isinstance(fields, dict):
            return False
        return any(
            isinstance(field, dict) and field.get("value") is not None
            for field in fields.values()
        )


@dataclass(frozen=True)
class PackageBuildResult:
    """What ``build_package`` produced: the immutable package and its manifest."""

    package: KnowledgePackage
    declaration_file_id: str
    refs: list[ObjectRef]


class KnowledgeService:
    """KNW-T02 package publication and the ``knowledge`` task entry.

    A package is a trusted internal product object, never a browser-supplied
    JSON document: the caller names published snapshots and a registered scope.
    """

    def __init__(
        self,
        repository: ProductRepository,
        files: FileService,
        *,
        clock: Callable[[], str] = utc_now,
        evaluator: CoverageEvaluator | None = None,
    ) -> None:
        self.repository = repository
        self.files = files
        self.clock = clock
        self.evaluator = evaluator or CoverageEvaluator()

    @property
    def provider_sends(self) -> int:
        """Package building never calls a provider; kept explicit for evidence."""
        return 0

    # -- task entry --------------------------------------------------------
    def handle(self, context: TaskContext) -> HandlerOutcome:
        task_input = context.task_input
        if not isinstance(task_input, KnowledgeInput):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the knowledge handler received another task type",
            )
        context.phase("acquire")
        game_context = self._context_for(task_input.game_context_id, context.user_scope_id)
        prior = self._task_prior_package(
            task_input.prior_package_id, game_context, context.user_scope_id
        )
        if prior is not None and self._coverage_complete(prior):
            # Nothing is missing for this scope: reuse the published package
            # instead of manufacturing an identical new version.
            return HandlerOutcome(
                state=TaskState.completed,
                domain_result=DomainResult(
                    domain=TaskType.knowledge,
                    code=CollectionResult.completed_scope.value,
                    input_revision=context.task_input_revision,
                    checks=[_rule_check(
                        "collection.reuse",
                        CheckStatus.pass_,
                        "the prior package already covers every declared cell; no"
                        " provider call was made",
                    )],
                ),
                result_refs=[ObjectRef(
                    entity_type="KnowledgePackage", entity_id=prior.id,
                )],
            )

        context.phase("collect")
        run = self._collect(
            context, game_context, prior,
            started_at=context.service.get_task(context.task_id).task.created_at,
        )

        snapshot_ids = self._published_snapshots(game_context.game_id)
        if run.sources:
            context.phase("extract")
            started_at = run.started_at
            writer = CollectionSnapshotWriter(
                self.repository, self.files, clock=self.clock
            )
            group, mapping_id = writer.write(
                task_id=context.task_id,
                run_id=f"run-{context.task_id}",
                game_series_id=game_context.game_id,
                user_scope_id=context.user_scope_id,
                started_at=started_at,
                collection_result=(
                    CollectionResult.partial_scope
                    if run.unknown_call
                    else self._collection_result(run)
                ),
                stop_reason=run.stop_reason,
                sources=list(run.sources.values()),
                gaps=self._remaining_gaps(run, task_input.prior_package_id),
            )
            imported = ImportPublisher(self.repository, self.files).publish(
                frozen=group,
                game_context_id=game_context.id,
                mapping_version="collector-import/1",
                identity_mapping_ids=[mapping_id],
                user_scope_id=context.user_scope_id,
                idempotency_key=(
                    f"collection-import:{context.task_id}:{context.task_input_revision}"
                ),
                created_by_task_id=context.task_id,
            )
            if imported.snapshot_id not in snapshot_ids:
                snapshot_ids.append(imported.snapshot_id)

        if not snapshot_ids:
            return self._apply_call_state(HandlerOutcome(
                state=TaskState.partial,
                domain_result=DomainResult(
                    domain=TaskType.knowledge,
                    code=CollectionResult.partial_scope.value,
                    input_revision=context.task_input_revision,
                    checks=[
                        _rule_check(
                            "package.input",
                            CheckStatus.unknown,
                            "no published import snapshot covers this game and the"
                            " collection produced no record; nothing was packaged",
                        ),
                        *run.checks(),
                    ],
                ),
                stop_reason=TaskStopReason(
                    kind="collection",
                    code=(
                        run.stop_reason.value
                        if run.stop_reason is not None
                        else "source_unavailable"
                    ),
                ),
                result_refs=[ObjectRef(
                    entity_type="GameContext", entity_id=game_context.id,
                )],
            ), run)

        context.phase("finalize")
        try:
            built = self.build_package(
                snapshot_ids,
                prior.id if prior is not None else None,
                KNOWLEDGE_TASK_SCOPE,
                game_context_id=task_input.game_context_id,
                user_scope_id=context.user_scope_id,
                idempotency_key=f"package:{context.task_id}:{context.task_input_revision}",
                collection_result=self._collection_result(run),
                stop_reason=run.stop_reason,
                collection_execution_ref=ObjectRef(
                    entity_type="TaskRun", entity_id=context.task_id,
                ),
            )
        except ContractError as error:
            if error.code is not ErrorCode.PRECONDITION_UNVERIFIED:
                raise
            # The snapshot is kept and its diagnostics stay readable; only the
            # "matched" package is withheld until identity/platform closes.
            return self._apply_call_state(HandlerOutcome(
                state=TaskState.partial,
                domain_result=DomainResult(
                    domain=TaskType.knowledge,
                    code=CollectionResult.partial_scope.value,
                    input_revision=context.task_input_revision,
                    checks=[
                        _rule_check("package.identity", CheckStatus.unknown, error.message),
                        *run.checks(),
                    ],
                ),
                stop_reason=TaskStopReason(kind="collection", code="source_unavailable"),
                result_refs=[
                    *(ObjectRef(entity_type="ImportSnapshot", entity_id=snapshot_id)
                      for snapshot_id in snapshot_ids),
                    ObjectRef(entity_type="GameContext", entity_id=game_context.id),
                ],
            ), run)

        package = built.package
        covered = self._coverage_complete(package) and not run.unknown_call
        outcome = HandlerOutcome(
            state=TaskState.completed if covered else TaskState.partial,
            domain_result=DomainResult(
                domain=TaskType.knowledge,
                code=(
                    CollectionResult.completed_scope.value
                    if covered
                    else CollectionResult.partial_scope.value
                ),
                input_revision=context.task_input_revision,
                checks=[*package.checks, *run.checks()],
            ),
            stop_reason=(
                None if run.stop_reason is None
                else TaskStopReason(kind="collection", code=run.stop_reason.value)
            ),
            result_refs=[
                ObjectRef(entity_type="KnowledgePackage", entity_id=package.id),
                ObjectRef(
                    entity_type="ManagedFile", entity_id=built.declaration_file_id,
                ),
                *(ObjectRef(entity_type="ImportSnapshot", entity_id=snapshot_id)
                  for snapshot_id in snapshot_ids),
            ],
        )
        if run.unknown_call:
            outcome.state = TaskState.partial
            outcome.error_code = ErrorCode.CALL_OUTCOME_UNKNOWN.value
            outcome.stop_reason = TaskStopReason(kind="execution", code="process_exit")
        return outcome

    @staticmethod
    def _apply_call_state(
        outcome: HandlerOutcome, run: "_CollectionRun"
    ) -> HandlerOutcome:
        """An unknown provider call dominates the execution outcome."""
        if run.unknown_call:
            outcome.state = TaskState.partial
            outcome.error_code = ErrorCode.CALL_OUTCOME_UNKNOWN.value
            outcome.stop_reason = TaskStopReason(kind="execution", code="process_exit")
        return outcome

    # -- collection --------------------------------------------------------
    def _task_prior_package(
        self, prior_package_id: str | None, game_context: GameContext, user_scope_id: str
    ) -> KnowledgePackage | None:
        series_id = package_series_id(game_context.game_id, KNOWLEDGE_TASK_SCOPE)
        if prior_package_id is not None:
            prior = self.get_package(
                prior_package_id, user_scope_id=user_scope_id, verify_artifacts=False
            )
            if prior.series_id != series_id:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the prior package belongs to another game or declared scope",
                )
            return prior
        with self.repository.unit_of_work() as uow:
            latest = uow.latest_for_series("KnowledgePackage", series_id)
        if latest is None:
            return None
        candidate = KnowledgePackage.model_validate_json(latest.payload_bytes)
        if candidate.publication_status is not PublicationStatus.published:
            return None
        return candidate

    def _initial_gaps(self, prior: KnowledgePackage | None) -> list[GapSpec]:
        if prior is None:
            return [GapSpec(
                key="coverage.seed",
                priority="P0",
                operation_scope=KNOWLEDGE_TASK_SCOPE,
                question="collect the declared public graphics facts for this game",
                expects=("settings", "configurations", "evidence", "environments"),
            )]
        gaps: list[GapSpec] = []
        for gap in [*prior.critical_gaps, *prior.noncritical_gaps]:
            if gap.status is not GapStatus.open:
                continue
            cell_key = next(
                (
                    check.check_id[len("coverage."):]
                    for check in gap.checks
                    if check.check_id.startswith("coverage.")
                ),
                None,
            )
            gaps.append(GapSpec(
                key=gap.id,
                priority=gap.priority.value,
                operation_scope=gap.operation_scope,
                question=gap.question,
                expects=_CELL_EXPECTS.get(cell_key or "", ()),
            ))
        return gaps

    def _collect(
        self,
        context: TaskContext,
        game_context: GameContext,
        prior: KnowledgePackage | None,
        *,
        started_at: str,
    ) -> "_CollectionRun":
        budget = context.budget
        planner = FollowupPlanner(max_queries_per_gap=budget.max_queries_per_gap)
        run = _CollectionRun(
            started_at=started_at,
            open_gaps={gap.key: gap for gap in self._initial_gaps(prior)},
        )
        seen: set[tuple[str, str, str]] = set()
        rounds_without_gain = 0
        for _round in range(budget.max_followup_rounds):
            planned = planner.plan_round(run.open_gaps.values(), used=run.used)
            if not planned:
                run.stop_reason = CollectionStopReason.coverage_reached
                break
            run.rounds += 1
            gained = 0
            stopped = False
            for step in planned:
                run.used[step.gap_key] = run.used.get(step.gap_key, 0) + 1
                try:
                    reply = context.invoke_provider(
                        kind="search",
                        provider_name="stub",
                        payload={
                            "query": step.query,
                            "operation_scope": step.operation_scope,
                        },
                    )
                except BudgetExhausted:
                    run.stop_reason = CollectionStopReason.budget_exhausted
                    run.budget_exhausted = True
                    stopped = True
                    break
                except UnknownCall:
                    run.unknown_call = True
                    stopped = True
                    break
                try:
                    urls = parse_search_reply(reply)
                except CollectionProtocolError as error:
                    run.errors.append(f"search {step.gap_key}: {error}")
                    continue
                step_gain = 0
                for url in urls:
                    result = self._read_and_extract(context, run, url, step, seen)
                    if result == "stop":
                        stopped = True
                        break
                    step_gain += result
                gained += step_gain
                if step_gain:
                    run.open_gaps.pop(step.gap_key, None)
                if stopped:
                    break
            if stopped:
                break
            if gained:
                rounds_without_gain = 0
                run.gained += gained
            else:
                rounds_without_gain += 1
                if rounds_without_gain >= 2:
                    run.stop_reason = CollectionStopReason.low_information_gain
                    break
        if run.stop_reason is None:
            run.stop_reason = (
                CollectionStopReason.coverage_reached
                if not run.open_gaps
                else CollectionStopReason.budget_exhausted
            )
        return run

    def _read_and_extract(
        self,
        context: TaskContext,
        run: "_CollectionRun",
        url: str,
        step: Any,
        seen: set[tuple[str, str, str]],
    ) -> int | str:
        """One read and one extraction per URL; several bad rows share both."""
        url = canonical_url(url)
        if url in run.read_urls:
            return 0
        if len(run.read_urls) >= context.budget.max_urls:
            run.stop_reason = CollectionStopReason.budget_exhausted
            run.budget_exhausted = True
            return "stop"
        try:
            read_reply = context.invoke_provider(
                kind="read", provider_name="stub", payload={"url": url},
            )
        except BudgetExhausted:
            run.stop_reason = CollectionStopReason.budget_exhausted
            run.budget_exhausted = True
            return "stop"
        except UnknownCall:
            run.unknown_call = True
            return "stop"
        run.read_urls.add(url)
        scope_id = source_scope_id(url)
        try:
            extract_reply = context.invoke_provider(
                kind="model",
                provider_name="stub",
                payload={
                    "url": url,
                    "document": (
                        read_reply.get("document")
                        if isinstance(read_reply, dict) else None
                    ),
                },
                requested_model="stub-model",
                prompt_version="collector-extract/1",
                output_schema_version="extraction/0.1",
            )
        except BudgetExhausted:
            run.stop_reason = CollectionStopReason.budget_exhausted
            run.budget_exhausted = True
            return "stop"
        except UnknownCall:
            run.unknown_call = True
            return "stop"
        try:
            records, errors = parse_extraction_reply(
                extract_reply, scope_id=scope_id
            )
        except CollectionProtocolError as error:
            run.errors.append(f"extract {scope_id}: {error}")
            return 0
        run.errors.extend(errors)
        retrieved_at = (
            extract_reply.get("retrieved_at")
            if isinstance(extract_reply, dict)
            and isinstance(extract_reply.get("retrieved_at"), str)
            else run.started_at
        )
        source = run.sources.get(scope_id)
        if source is None:
            source = CollectedSource(
                scope_id=scope_id,
                url=url,
                revision_id=source_revision_id(url),
                retrieved_at=retrieved_at,
                model_requested="stub-model",
                raw_extraction=(
                    extract_reply if isinstance(extract_reply, dict) else {}
                ),
            )
            run.sources[scope_id] = source
        gained = 0
        for record in records:
            key = (scope_id, record.record_type, record.local_id)
            if key in seen:
                continue
            seen.add(key)
            source.records.append(record)
            if record.record_type in step.expects:
                gained += 1
        return gained

    @staticmethod
    def _collection_result(run: "_CollectionRun") -> CollectionResult:
        if run.stop_reason is CollectionStopReason.coverage_reached and not run.open_gaps:
            return CollectionResult.completed_scope
        return CollectionResult.partial_scope

    def _remaining_gaps(
        self, run: "_CollectionRun", prior_package_id: str | None
    ) -> list[CollectedGap]:
        gaps = [
            CollectedGap(
                gap_id=gap.key if len(gap.key) <= 64 else gap.key[:64],
                priority=gap.priority,
                question=gap.question,
                reason="not closed by the collected evidence",
                field="coverage",
                status="open",
            )
            for gap in run.open_gaps.values()
        ]
        for index, error in enumerate(run.errors):
            gaps.append(CollectedGap(
                gap_id=f"read-error-{index + 1}",
                priority="P1",
                question="re-read the source or correct the extraction",
                reason=error,
                field="record",
                status="open",
            ))
        return gaps

    def precheck_collection(
        self, task_input: Any, *, user_scope_id: str
    ) -> GameContext:
        """Guards that must fail before a task key is consumed (error §4.2).

        A missing/foreign context, an unknown platform, an invisible game or a
        budget that permits no call is a pure conflict: no task is registered.
        """
        typed = (
            task_input
            if isinstance(task_input, KnowledgeInput)
            else KnowledgeInput.model_validate(task_input)
        )
        game_context = self._context_for(typed.game_context_id, user_scope_id)
        self._game_for_context(game_context, user_scope_id)
        if game_context.platform.value_state is not ValueState.known:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the game context has no declared platform; a matched package"
                " cannot be published",
            )
        budget = typed.budget
        if not (
            budget.max_urls
            or budget.max_searches
            or budget.max_read_calls
            or budget.max_model_calls
        ):
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the budget permits no call; nothing was registered",
            )
        return game_context

    # -- build -------------------------------------------------------------
    def build_package(
        self,
        import_snapshot_ids: Sequence[str],
        prior_package_id: str | None,
        declared_scope: str,
        *,
        game_context_id: str,
        user_scope_id: str,
        idempotency_key: str,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        image_required: bool = False,
        collection_result: CollectionResult | None = None,
        stop_reason: CollectionStopReason | None = None,
        collection_execution_ref: ObjectRef | None = None,
        hooks: PublishHooks | None = None,
    ) -> PackageBuildResult:
        if not import_snapshot_ids:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a package needs at least one published import snapshot",
            )
        if len(set(import_snapshot_ids)) != len(import_snapshot_ids):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a package names each import snapshot once",
            )
        if not is_registered_scope(declared_scope):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                f"unregistered operation scope: {declared_scope}",
                field_errors=[FieldError(
                    path="declared_scope", code="enum", message="unregistered scope",
                )],
            )
        snapshot_ids = sorted(import_snapshot_ids)
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": "POST",
            "route": PACKAGE_ROUTE,
            "user_scope_id": user_scope_id,
            "game_context_id": game_context_id,
            "import_snapshot_ids": snapshot_ids,
            "prior_package_id": prior_package_id,
            "declared_scope": declared_scope,
            "image_required": image_required,
            "collection_result": None if collection_result is None else collection_result.value,
            "stop_reason": None if stop_reason is None else stop_reason.value,
            "collection_execution_ref": (
                None if collection_execution_ref is None
                else collection_execution_ref.model_dump(mode="json")
            ),
        })).hexdigest()
        package_id = uuid5(
            _PACKAGE_NS,
            f"{user_scope_id}:{PACKAGE_ROUTE}:{idempotency_key}:{request_hash}",
        ).hex

        # Every guard runs before ``files.publish``: a pure conflict never
        # consumes the idempotency key (error §4.2).
        game_context = self._context_for(game_context_id, user_scope_id)
        game = self._game_for_context(game_context, user_scope_id)
        self._require_publishable_identity(game)
        platform_scope = self._platform_scope(game_context)
        series_id = package_series_id(game_context.game_id, declared_scope)
        prior = self._prior_package(prior_package_id, series_id)
        records, diagnostics = self._snapshot_members(snapshot_ids, game_context.game_id)
        self._require_dependency_closure(records)

        cells, gaps = self.evaluator.evaluate(
            package_id=package_id,
            declared_scope=declared_scope,
            records=records,
            diagnostics=diagnostics,
            image_required=image_required,
        )
        if collection_result is CollectionResult.completed_scope and not all(
            cell.status in (CoverageStatus.covered, CoverageStatus.not_applicable)
            for cell in cells
        ):
            # 02 §4: completed_scope needs every declared cell covered or
            # provably not applicable; a run that stopped early cannot claim it.
            collection_result = CollectionResult.partial_scope
        critical = [gap for gap in gaps if gap.priority is GapPriority.P0]
        noncritical = [gap for gap in gaps if gap.priority is not GapPriority.P0]
        package = KnowledgePackage(
            id=package_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=self.clock(),
            created_by_task_id=None,
            series_id=series_id,
            version=1,
            game_id=game_context.game_id,
            game_revision_id=game_context.game_revision_id,
            platform_scope=platform_scope,
            parent_package_id=prior.id if prior is not None else None,
            import_snapshot_ids=snapshot_ids,
            source_revision_ids=sorted({record.source_revision_id for record in records}),
            record_snapshot_ids=sorted(record.id for record in records),
            setting_catalog=self._group_refs(package_id, records, "settings"),
            configuration_templates=self._group_refs(package_id, records, "configurations"),
            measurement_evidence=self._group_refs(package_id, records, "evidence"),
            constraints=self._group_refs(package_id, records, "constraints"),
            diagnostic_record_ids=sorted(record.id for record in diagnostics),
            coverage_matrix=cells,
            critical_gaps=critical,
            noncritical_gaps=noncritical,
            checks=self._package_checks(
                snapshot_ids, records, diagnostics, cells, collection_result,
            ),
            relation_ids=[],
            collection_result=collection_result,
            stop_reason=stop_reason,
            collection_execution_ref=collection_execution_ref,
            publication_status=PublicationStatus.published,
            declared_scope=declared_scope,
            checked_at=None,
        )
        declaration = {
            "kind": "knowledge_package",
            "rule_version": PACKAGE_RULE_VERSION,
            "package_id": package_id,
            "series_id": series_id,
            "game_id": package.game_id,
            "game_revision_id": package.game_revision_id,
            "game_context_id": game_context_id,
            "declared_scope": declared_scope,
            "import_snapshot_ids": snapshot_ids,
            "prior_package_id": prior_package_id,
            "record_snapshot_ids": package.record_snapshot_ids,
            "diagnostic_record_ids": package.diagnostic_record_ids,
            "coverage_keys": [cell.key for cell in cells],
            "image_required": image_required,
            "request_hash": request_hash,
        }
        published = self.files.publish(
            kind="import",
            files={
                f"packages/{package_id}/{_DECLARATION_FILE}": canonical_json(declaration),
            },
            user_scope_id=user_scope_id,
            method="POST",
            canonical_route=PACKAGE_ROUTE,
            key=idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_package(
                uow,
                managed=managed,
                package=package,
                prior=prior,
                snapshot_ids=snapshot_ids,
                source_revision_ids=package.source_revision_ids,
                record_ids=package.record_snapshot_ids,
            ),
            hooks=hooks,
        )
        stored = self.repository.get_object("KnowledgePackage", package_id)
        return PackageBuildResult(
            package=KnowledgePackage.model_validate_json(stored.payload_bytes),
            declaration_file_id=published.file_ids[0],
            refs=[ObjectRef(entity_type="KnowledgePackage", entity_id=package_id)],
        )

    def _write_package(
        self,
        uow: UnitOfWork,
        *,
        managed: Sequence[Any],
        package: KnowledgePackage,
        prior: KnowledgePackage | None,
        snapshot_ids: Sequence[str],
        source_revision_ids: Sequence[str],
        record_ids: Sequence[str],
    ) -> list[ObjectRef]:
        existing = uow.find_object("KnowledgePackage", package.id)
        if existing is not None:
            return [ObjectRef(entity_type="KnowledgePackage", entity_id=package.id)]
        if prior is not None:
            stored = uow.find_object("KnowledgePackage", prior.id)
            if stored is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID, "the prior package no longer exists"
                )
            current = KnowledgePackage.model_validate_json(stored.payload_bytes)
            if current.series_id != package.series_id:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the prior package belongs to another game or declared scope",
                )
        # The version is assigned under the write lock; two concurrent new
        # packages of one series can never share a version.
        row = uow.query(
            "SELECT COALESCE(MAX(version), 0) AS last FROM objects"
            " WHERE entity_type='KnowledgePackage' AND series_id=?",
            (package.series_id,),
        )[0]
        final = package.model_copy(update={
            "version": int(row["last"]) + 1,
            "created_at": self.clock(),
        })
        uow.insert_versioned("KnowledgePackage", final)
        package_ref = ObjectRef(entity_type="KnowledgePackage", entity_id=final.id)
        for snapshot_id in snapshot_ids:
            uow.insert_reference(final.id, "import_snapshot", ObjectRef(
                entity_type="ImportSnapshot", entity_id=snapshot_id,
            ))
        for revision_id in source_revision_ids:
            uow.insert_reference(final.id, "source_revision", ObjectRef(
                entity_type="SourceRevision", entity_id=revision_id,
            ))
        for record_id in record_ids:
            uow.insert_reference(final.id, "record", ObjectRef(
                entity_type="ImportedRecord", entity_id=record_id,
            ))
        if prior is not None:
            uow.insert_reference(final.id, "parent_package", ObjectRef(
                entity_type="KnowledgePackage", entity_id=prior.id,
            ))
        uow.record_domain_event(
            target_type="KnowledgePackage",
            target_id=final.id,
            dimension="publication",
            sequence=1,
            previous_state=None,
            next_state=PublicationStatus.published.value,
            cause="package_published",
            related_refs=[package_ref, *(
                ObjectRef(entity_type="ImportSnapshot", entity_id=snapshot_id)
                for snapshot_id in snapshot_ids
            )],
            created_at=self.clock(),
        )
        return [package_ref]

    # -- reads -------------------------------------------------------------
    def get_package(
        self,
        package_id: str,
        *,
        user_scope_id: str,
        verify_artifacts: bool = True,
    ) -> KnowledgePackage:
        stored = self.repository.find_object("KnowledgePackage", package_id)
        if stored is None:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the requested package is not available"
            )
        package = KnowledgePackage.model_validate_json(stored.payload_bytes)
        if package.publication_status is not PublicationStatus.published:
            # An unpublished package exists only for the player whose
            # declaration produced it; everyone else sees nothing.
            if not self._caller_declared_package(package, user_scope_id):
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "the requested package is not available",
                )
        if verify_artifacts:
            self._verify_package_artifacts(package)
        return package

    def _caller_declared_package(
        self, package: KnowledgePackage, user_scope_id: str
    ) -> bool:
        with self.repository.unit_of_work() as uow:
            for snapshot_id in package.import_snapshot_ids:
                row = uow.query(
                    "SELECT declaration_file_id FROM import_snapshots"
                    " WHERE import_snapshot_id=?",
                    (snapshot_id,),
                )
                if not row:
                    continue
                stored = uow.find_object("ManagedFile", row[0]["declaration_file_id"])
                if stored is not None and stored.payload.get("user_scope_id") == user_scope_id:
                    return True
        return False

    def _verify_package_artifacts(self, package: KnowledgePackage) -> None:
        """A consumed package must still locate readable originals (state §8.2)."""
        file_ids: list[str] = []
        with self.repository.unit_of_work() as uow:
            for snapshot_id in package.import_snapshot_ids:
                rows = uow.query(
                    "SELECT target_id FROM object_references WHERE owner_id=?"
                    " AND target_type='ManagedFile' ORDER BY target_id",
                    (snapshot_id,),
                )
                file_ids.extend(row["target_id"] for row in rows)
        for file_id in file_ids:
            self.files.read_managed_file(file_id)

    # -- helpers -----------------------------------------------------------
    def _context_for(self, context_id: str, user_scope_id: str) -> GameContext:
        stored = self.repository.find_object("GameContext", context_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced game context does not exist",
                field_errors=[FieldError(
                    path="game_context_id", code="reference", message="unknown context",
                )],
            )
        installation_id = stored.payload.get("installation_id")
        if installation_id is not None:
            with self.repository.unit_of_work() as uow:
                installation = uow.latest_for_series("GameInstallation", installation_id)
            if installation is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the context names an installation that does not exist",
                )
            if installation.payload.get("user_scope_id") != user_scope_id:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "the referenced game context is not available",
                )
        return GameContext.model_validate_json(stored.payload_bytes)

    def _game_for_context(self, game_context: GameContext, user_scope_id: str) -> Game:
        stored = self.repository.find_object("Game", game_context.game_revision_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the game context names a game revision that does not exist",
            )
        game = Game.model_validate_json(stored.payload_bytes)
        if game.series_id != game_context.game_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the game revision does not belong to the context's game",
            )
        if (
            game.identity_status is GameIdentityStatus.provisional
            and game.declaring_user_scope_id is not None
            and game.declaring_user_scope_id != user_scope_id
        ):
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the referenced game is not available"
            )
        return game

    @staticmethod
    def _require_publishable_identity(game: Game) -> None:
        if game.identity_status is not GameIdentityStatus.verified:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the game identity is not verified; the sources and their gaps are"
                " kept, but no matched package is published",
            )

    @staticmethod
    def _platform_scope(game_context: GameContext) -> list[str]:
        if game_context.platform.value_state is not ValueState.known:
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "the game context has no declared platform; a matched package"
                " cannot be published",
            )
        return [str(game_context.platform.value)]

    def _prior_package(
        self, prior_package_id: str | None, series_id: str
    ) -> KnowledgePackage | None:
        if prior_package_id is None:
            return None
        stored = self.repository.find_object("KnowledgePackage", prior_package_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the prior package does not exist",
                field_errors=[FieldError(
                    path="prior_package_id", code="reference", message="unknown package",
                )],
            )
        prior = KnowledgePackage.model_validate_json(stored.payload_bytes)
        if prior.publication_status is not PublicationStatus.published:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the prior package is not published and cannot be a parent",
            )
        if prior.series_id != series_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the prior package belongs to another game or declared scope",
            )
        return prior

    def _snapshot_members(
        self, snapshot_ids: Sequence[str], game_id: str
    ) -> tuple[list[ImportedRecord], list[ImportedRecord]]:
        records: list[ImportedRecord] = []
        diagnostics: list[ImportedRecord] = []
        with self.repository.unit_of_work() as uow:
            for snapshot_id in snapshot_ids:
                row = uow.query(
                    "SELECT import_status, mapped_game_id FROM import_snapshots"
                    " WHERE import_snapshot_id=?",
                    (snapshot_id,),
                )
                if not row:
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        "the referenced import snapshot does not exist",
                        field_errors=[FieldError(
                            path="import_snapshot_ids", code="reference",
                            message="unknown snapshot",
                        )],
                    )
                if row[0]["import_status"] != PublicationStatus.published.value:
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        "an unpublished import snapshot cannot be a package member",
                    )
                if row[0]["mapped_game_id"] is None:
                    raise ContractError(
                        ErrorCode.PRECONDITION_UNVERIFIED,
                        "the import snapshot's game identity is unresolved; its"
                        " diagnostics are kept but no matched package is published",
                    )
                if row[0]["mapped_game_id"] != game_id:
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        "the import snapshot belongs to another game",
                    )
                keys = uow.query(
                    "SELECT record_snapshot_id, usable FROM imported_record_keys"
                    " WHERE import_snapshot_id=? ORDER BY rowid",
                    (snapshot_id,),
                )
                for key in keys:
                    stored = uow.find_object(
                        "ImportedRecord", key["record_snapshot_id"]
                    )
                    if stored is None:
                        raise ContractError(
                            ErrorCode.REFERENCE_INVALID,
                            "an import names a record that does not exist",
                        )
                    record = ImportedRecord.model_validate_json(stored.payload_bytes)
                    (records if key["usable"] else diagnostics).append(record)
        return records, diagnostics

    @staticmethod
    def _require_dependency_closure(records: Sequence[ImportedRecord]) -> None:
        # Import-time isolation already removes every record whose typed
        # references did not resolve; a member here is dependency-closed.  The
        # check is repeated with the importer's own reference table so a
        # hand-edited row cannot slip through.
        by_key = {
            (record.capture_scope_id, record.record_type, record.local_record_id): record
            for record in records
        }
        for record in records:
            for target_type, local_id, path in record_payload_references(
                record.record_type, record.payload
            ):
                if (record.capture_scope_id, target_type, local_id) not in by_key:
                    raise ContractError(
                        ErrorCode.REFERENCE_INVALID,
                        f"record {record.local_record_id!r} has an unresolved"
                        f" dependency at {path} -> {target_type} {local_id!r}",
                    )

    @staticmethod
    def _group_refs(
        package_id: str, records: Sequence[ImportedRecord], record_type: str
    ) -> list[KnowledgeRecordRef]:
        return [
            _record_ref(package_id, record)
            for record in records if record.record_type == record_type
        ]

    def _package_checks(
        self,
        snapshot_ids: Sequence[str],
        records: Sequence[ImportedRecord],
        diagnostics: Sequence[ImportedRecord],
        cells: Sequence[CoverageCell],
        collection_result: CollectionResult | None,
    ) -> list[CheckResult]:
        covered = all(
            cell.status in (CoverageStatus.covered, CoverageStatus.not_applicable)
            for cell in cells
        )
        return [
            _rule_check(
                "package.identity", CheckStatus.pass_,
                "the context's game revision is verified and every snapshot maps to"
                " the same game",
            ),
            _rule_check(
                "package.snapshot", CheckStatus.pass_,
                f"{len(snapshot_ids)} published snapshot(s);"
                f" {len(records)} usable record(s), {len(diagnostics)} diagnostic record(s)",
            ),
            _rule_check(
                "package.dependency_closure", CheckStatus.pass_,
                "every selected member resolves its typed references inside its"
                " capture scope",
            ),
            _rule_check(
                "package.private_isolation", CheckStatus.pass_,
                "the package holds only KnowledgeRecordRef members; sessions,"
                " preferences, summaries and outcomes never enter it",
            ),
            _rule_check(
                "package.coverage",
                CheckStatus.pass_ if covered else CheckStatus.unknown,
                "every declared cell is covered"
                if covered
                else "at least one declared cell is unknown or partially covered",
            ),
            _rule_check(
                "package.execution_fact",
                CheckStatus.not_run if collection_result is None else CheckStatus.pass_,
                "no single product collection execution; collection_result,"
                " stop_reason and collection_execution_ref are null"
                if collection_result is None
                else "the package records the one collection execution that produced it",
            ),
        ]

    @staticmethod
    def _coverage_complete(package: KnowledgePackage) -> bool:
        """``completed_scope`` is a coverage result, not an execution claim.

        It requires every declared cell to be covered or provably not
        applicable and no critical gap left open; it never depends on a
        product collection execution (an imported package can be complete).
        """
        return (
            all(
                cell.status in (CoverageStatus.covered, CoverageStatus.not_applicable)
                for cell in package.coverage_matrix
            )
            and not any(gap.status is GapStatus.open for gap in package.critical_gaps)
        )

    def _published_snapshots(self, game_id: str) -> list[str]:
        with self.repository.unit_of_work() as uow:
            rows = uow.query(
                "SELECT import_snapshot_id FROM import_snapshots"
                " WHERE import_status='published' AND mapped_game_id=?"
                " ORDER BY created_at, rowid",
                (game_id,),
            )
        return [row["import_snapshot_id"] for row in rows]


__all__ = [
    "IDENTITY_RULE_VERSION",
    "IMAGE_ADAPTERS_VERIFIED",
    "IMAGE_DIMENSION",
    "IMPORT_IDENTITY_ROUTE",
    "IdentityService",
    "KNOWLEDGE_TASK_SCOPE",
    "KnowledgeService",
    "PACKAGE_ROUTE",
    "PACKAGE_RULE_VERSION",
    "PackageBuildResult",
    "CoverageEvaluator",
    "SELECTION_ROUTE",
    "SELECTION_RULE_VERSION",
    "image_adapter_status",
]
