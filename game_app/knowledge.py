"""Public game identity, declaration slots and purpose-level reuse (02).

Only local data is read: the catalog never goes online, probes hardware or calls
a model.  Identity is written as provisional until evidence closes it; two
series are never merged on a name alone.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Callable, Iterable, Sequence

from game_app.contracts.common import (
    CheckResult,
    EvidenceRef,
    FieldValue,
    MutationContext,
    Provenance,
    ResourceResult,
)
from game_app.contracts.device import AdapterCapabilities, GameContext
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    GameIdentityStatus,
    PublicationStatus,
    ValueState,
)
from game_app.contracts.knowledge import (
    is_registered_scope,
    Game,
    GameCatalogQuery,
    GameChoice,
    GameChoicePage,
    GameSelection,
    GameSelectionRequest,
    IdentityMapping,
    IdentityResolution,
    ImportIdentityMappingRequest,
    KnowledgePackage,
    ReuseDecision,
)
from game_app.contracts.primitives import ObjectRef, new_id, utc_now
from game_app.errors import ContractError, ErrorCode, FieldError
from game_app.files import FileService, PublishHooks
from game_app.repository import ProductRepository, StoredObject, UnitOfWork, canonical_json

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


__all__ = [
    "IDENTITY_RULE_VERSION",
    "IMPORT_IDENTITY_ROUTE",
    "IdentityService",
    "SELECTION_ROUTE",
    "SELECTION_RULE_VERSION",
]
