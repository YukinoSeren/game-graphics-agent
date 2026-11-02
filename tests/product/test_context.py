"""DEV-T02: installations, local catalogs and context preparation (01 §5.1/§5.2).

Everything runs against temporary databases and directories: no probe, no game,
no network.  A path is never invented, and a setting entry is never adopted.
"""

from __future__ import annotations

import json
import threading

import pytest
from pydantic import ValidationError

from game_app.contracts.common import CheckResult, EvidenceRef, FieldValue, MutationContext, Provenance
from game_app.contracts.device import (
    AdapterCapabilities,
    CreateInstallationRequest,
    GameContext,
    GameInstallation,
    InstallationCatalogQuery,
    LocalSettingChoice,
    RegisterLocalSettingRequest,
)
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    GameIdentityStatus,
    ValueState,
)
from game_app.contracts.knowledge import Game, GameSelectionRequest
from game_app.device import ContextService
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService
from game_app.knowledge import IdentityService
from game_app.repository import ProductRepository

USER = "a" * 32
OTHER_USER = "b" * 32
PLATFORM = "Windows PC"


def _uid(value: int) -> str:
    return f"{value:032x}"


def _now() -> str:
    return "2026-11-02T00:00:00+00:00"


def _ctx(key: str = "ctx-key", user: str = USER) -> MutationContext:
    return MutationContext(user_scope_id=user, idempotency_key=key, request_id=_uid(999))


def _check(status: CheckStatus = CheckStatus.pass_) -> CheckResult:
    return CheckResult(
        check_id="identity.evidence",
        definition_version="identity-checks/0.4",
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else _now(),
        method="synthetic fixture check",
    )


def _declared_platform(value: str = PLATFORM) -> FieldValue[str]:
    return FieldValue[str](
        value_state=ValueState.known,
        value=value,
        acquisition_status=AcquisitionStatus.succeeded,
        provenance=[Provenance(
            method=AcquisitionMethod.user_provided,
            observed_at=None,
            recorded_at=_now(),
            evidence_refs=[EvidenceRef(
                kind="artifact",
                file_id=_uid(500),
                content_hash="e" * 64,
                locator="selection/request.json",
            )],
            note="player-declared platform",
        )],
    )


def _unknown_platform() -> FieldValue[str]:
    return FieldValue[str](
        value_state=ValueState.unknown,
        value=None,
        unknown_reason="not_provided",
        acquisition_status=AcquisitionStatus.not_attempted,
    )


def _game(uid: int = 100, *, name: str = "Synthetic Game") -> Game:
    return Game(
        id=_uid(uid),
        schema_version="product-contract/0.4",
        created_at=_now(),
        created_by_task_id=None,
        series_id=_uid(uid + 1),
        version=1,
        canonical_name=name,
        aliases=[],
        platform_scope=[PLATFORM],
        identity_status=GameIdentityStatus.verified,
        identity_checks=[_check()],
        declaring_user_scope_id=None,
    )


def _public_context(
    game: Game,
    *,
    uid: int = 200,
    platform: FieldValue[str] | None = None,
) -> GameContext:
    return GameContext(
        id=_uid(uid),
        schema_version="product-contract/0.4",
        created_at=_now(),
        created_by_task_id=None,
        series_id=_uid(uid),
        version=1,
        game_id=game.series_id,
        game_revision_id=game.id,
        installation_id=None,
        platform=platform or _declared_platform(),
        build_identifiers=[],
        process_candidates=[],
        selected_process=None,
        adapter_id=None,
        adapter_version=None,
        capabilities=_no_adapter_capabilities(),
        checks=[_not_run("context.build")],
    )


def _no_adapter_capabilities() -> AdapterCapabilities:
    return AdapterCapabilities(
        can_identify=_not_run("adapter.identify"),
        can_read_config=_not_run("adapter.read_config"),
        can_bind_capture=_not_run("adapter.bind_capture"),
    )


def _not_run(check_id: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version="context-checks/0.4",
        status=CheckStatus.not_run,
        input_refs=[],
        evidence_refs=[],
        checked_at=None,
        method="no verified adapter",
    )


def _service(tmp_path) -> tuple[ContextService, ProductRepository]:
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    service = ContextService(repository, FileService(repository, tmp_path / "managed"))
    return service, repository


def _seed(repository: ProductRepository, game: Game, context: GameContext) -> None:
    with repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
        uow.insert_versioned("GameContext", context)


def _count(repository: ProductRepository, entity_type: str) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def _refs_types(result) -> list[str]:
    return [ref.entity_type for ref in result.refs]


def installation_ids_of(result) -> set[str]:
    """The stable installation id is the id of its first revision object."""
    return {ref.entity_id for ref in result.refs if ref.entity_type == "GameInstallation"}


# --- prepare_game_context ----------------------------------------------------


def test_prepare_game_context_writes_nothing_and_invents_nothing(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    _seed(repository, game, _public_context(game))
    before = _count(repository, "GameContext")

    candidate = service.prepare_game_context(
        game_revision_id=game.id,
        platform=None,
        installation_id=None,
    )

    assert isinstance(candidate, GameContext)
    assert candidate.game_id == game.series_id
    assert candidate.game_revision_id == game.id
    assert candidate.installation_id is None
    assert candidate.platform.value_state is ValueState.unknown
    assert candidate.build_identifiers == []
    assert candidate.process_candidates == []
    assert candidate.selected_process is None
    assert candidate.adapter_id is None and candidate.adapter_version is None
    assert candidate.capabilities.can_read_config.status is CheckStatus.not_run
    assert _count(repository, "GameContext") == before


def test_prepare_game_context_rejects_an_unknown_game_revision(tmp_path):
    service, repository = _service(tmp_path)

    with pytest.raises(ContractError) as caught:
        service.prepare_game_context(
            game_revision_id=_uid(404), platform=None, installation_id=None
        )
    assert caught.value.code is ErrorCode.REFERENCE_INVALID


def test_prepare_game_context_refuses_a_platform_without_evidence(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    _seed(repository, game, _public_context(game))

    with pytest.raises(ContractError) as caught:
        service.prepare_game_context(
            game_revision_id=game.id,
            platform=PLATFORM,
            installation_id=None,
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED


def test_prepare_game_context_accepts_a_declaration_evidence_ref(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    _seed(repository, game, _public_context(game))
    evidence = EvidenceRef(
        kind="artifact",
        file_id=_uid(600),
        content_hash="f" * 64,
        locator="declarations/context.json",
    )

    candidate = service.prepare_game_context(
        game_revision_id=game.id,
        platform=PLATFORM,
        installation_id=None,
        declaration_ref=evidence,
    )

    assert candidate.platform.value_state is ValueState.known
    assert candidate.platform.value == PLATFORM
    assert candidate.platform.provenance[0].evidence_refs == [evidence]


def test_prepare_game_context_base_must_share_series_and_installation(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    other = _game(uid=300, name="Other Game")
    base = _public_context(game, uid=201)
    other_base = _public_context(other, uid=301)
    _seed(repository, game, base)
    with repository.unit_of_work() as uow:
        uow.insert_versioned("Game", other)
        uow.insert_versioned("GameContext", other_base)

    # Another game's context is not a base for this series.
    with pytest.raises(ContractError) as caught:
        service.prepare_game_context(
            game_revision_id=game.id,
            platform=None,
            installation_id=None,
            base_context_id=other_base.id,
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED

    # A context of another installation is not a base either.
    with pytest.raises(ContractError) as caught:
        service.prepare_game_context(
            game_revision_id=game.id,
            platform=None,
            installation_id=_uid(777),
            base_context_id=base.id,
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED


def test_prepare_game_context_carries_declared_background_from_base(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    base = _public_context(game, uid=202)
    _seed(repository, game, base)
    before = json.dumps(
        repository.get_object("GameContext", base.id).payload, sort_keys=True
    )

    candidate = service.prepare_game_context(
        game_revision_id=game.id,
        platform=None,
        installation_id=None,
        base_context_id=base.id,
    )

    assert candidate.id != base.id
    assert candidate.platform == base.platform
    assert candidate.checks == base.checks
    # The base context is read-only input, never rewritten.
    after = json.dumps(
        repository.get_object("GameContext", base.id).payload, sort_keys=True
    )
    assert after == before


# --- installations -----------------------------------------------------------


def _create(service, context: GameContext, key: str, label: str | None = None, user: str = USER):
    return service.create_installation(
        CreateInstallationRequest(game_context_id=context.id, display_label=label),
        _ctx(key, user),
    )


def test_two_installations_for_the_same_game_are_not_merged(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    context = _public_context(game)
    _seed(repository, game, context)

    first = _create(service, context, "install-1", label="Steam copy")
    second = _create(service, context, "install-2", label="Steam copy")

    assert _refs_types(first) == ["InstallationRegistration", "GameInstallation", "GameContext"]
    assert _refs_types(second) == ["InstallationRegistration", "GameInstallation", "GameContext"]
    installation_ids = {
        ref.entity_id for ref in first.refs + second.refs if ref.entity_type == "GameInstallation"
    }
    assert len(installation_ids) == 2
    page = service.list_installations(
        InstallationCatalogQuery(game_id=game.series_id, cursor=None), user_scope_id=USER
    )
    assert len(page.choices) == 2
    assert [choice.display_label for choice in page.choices] == ["Steam copy", "Steam copy"]
    assert len({choice.installation_id for choice in page.choices}) == 2


def test_installation_records_are_honest_about_unknown_paths(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    context = _public_context(game)
    _seed(repository, game, context)

    result = _create(service, context, "install-unknown")
    installation_ref = next(
        ref for ref in result.refs if ref.entity_type == "GameInstallation"
    )
    installation = GameInstallation.model_validate_json(
        repository.get_object("GameInstallation", installation_ref.entity_id).payload_bytes
    )
    new_context = GameContext.model_validate_json(
        repository.get_object(
            "GameContext",
            next(ref for ref in result.refs if ref.entity_type == "GameContext").entity_id,
        ).payload_bytes
    )

    assert installation.origin == "user_provided"
    assert installation.private_path_ref is None
    assert installation.platform == context.platform
    assert installation.series_id == installation.id
    assert new_context.installation_id == installation.series_id
    # No path, no observation, no device snapshot and no probe were invented.
    assert new_context.capabilities.can_read_config.status is CheckStatus.not_run
    assert _count(repository, "ConfigurationObservation") == 0
    assert _count(repository, "DeviceSnapshot") == 0


def test_generated_label_is_a_stable_short_reference(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    context = _public_context(game)
    _seed(repository, game, context)

    _create(service, context, "install-label")
    page = service.list_installations(
        InstallationCatalogQuery(game_id=game.series_id, cursor=None), user_scope_id=USER
    )

    label = page.choices[0].display_label
    assert label.startswith("本机记录")
    assert page.choices[0].installation_id[:8] in label


def test_create_installation_is_idempotent_per_key_and_rejects_new_content(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    context = _public_context(game)
    _seed(repository, game, context)

    first = _create(service, context, "install-key", label="A")
    replay = _create(service, context, "install-key", label="A")
    assert replay.model_dump() == first.model_dump()

    with pytest.raises(ContractError) as caught:
        _create(service, context, "install-key", label="B")
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _count(repository, "GameInstallation") == 1


def test_same_key_with_new_content_conflicts_before_reference_checks(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    context = _public_context(game)
    _seed(repository, game, context)
    _create(service, context, "install-key")

    # A registered key keeps its conflict meaning even if the new request is
    # also malformed; it never degrades into a reference error.
    with pytest.raises(ContractError) as caught:
        service.create_installation(
            CreateInstallationRequest(game_context_id=_uid(404), display_label=None),
            _ctx("install-key"),
        )
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _count(repository, "GameInstallation") == 1


def test_create_installation_rejects_an_unknown_context_without_preparing_a_file(tmp_path):
    service, repository = _service(tmp_path)

    with pytest.raises(ContractError) as caught:
        service.create_installation(
            CreateInstallationRequest(game_context_id=_uid(404), display_label=None),
            _ctx("install-missing"),
        )
    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    assert _count(repository, "GameInstallation") == 0
    assert service.files.get_operation_by_key(
        USER, "POST", "/api/installations", "install-missing"
    ) is None


def test_list_installations_paginates_and_isolates_users(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    context = _public_context(game)
    _seed(repository, game, context)
    for index in range(3):
        _create(service, context, f"install-{index}", label=f"copy-{index}")

    first = service.list_installations(
        InstallationCatalogQuery(game_id=game.series_id, cursor=None, limit=2),
        user_scope_id=USER,
    )
    assert len(first.choices) == 2 and first.next_cursor is not None
    second = service.list_installations(
        InstallationCatalogQuery(game_id=game.series_id, cursor=first.next_cursor, limit=2),
        user_scope_id=USER,
    )
    assert len(second.choices) == 1 and second.next_cursor is None
    assert not {c.installation_id for c in first.choices} & {c.installation_id for c in second.choices}

    assert service.list_installations(
        InstallationCatalogQuery(game_id=game.series_id, cursor=None), user_scope_id=OTHER_USER
    ).choices == []


def test_selection_accepts_a_listed_installation_and_rejects_a_stale_revision(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    context = _public_context(game)
    _seed(repository, game, context)
    created = _create(service, context, "install-select")
    installation_id = installation_ids_of(created).pop()

    identity = IdentityService(repository, service.files)
    accepting = identity.select_game(
        GameSelectionRequest(
            raw_name=game.canonical_name,
            selected_game_revision_id=game.id,
            platform=PLATFORM,
            installation_id=installation_id,
            expected_installation_revision=1,
        ),
        user_scope_id=USER,
        idempotency_key="select-1",
        request_id=_uid(998),
    )
    assert "GameContext" in _refs_types(accepting)

    with pytest.raises(ContractError) as caught:
        identity.select_game(
            GameSelectionRequest(
                raw_name=game.canonical_name,
                selected_game_revision_id=game.id,
                platform=PLATFORM,
                installation_id=installation_id,
                expected_installation_revision=2,
            ),
            user_scope_id=USER,
            idempotency_key="select-2",
            request_id=_uid(997),
        )
    assert caught.value.code is ErrorCode.INSTALLATION_REVISION_CONFLICT


# --- local setting registration ---------------------------------------------


def _register(
    service,
    installation_id: str,
    *,
    key: str,
    raw_name: str = "自定义设置甲",
    menu_path: list[str] | None = None,
    identity_mode: str = "auto",
    reuse_id: str | None = None,
    supersedes_id: str | None = None,
    raw_text: str = "",
    user: str = USER,
):
    return service.register_local_setting(
        RegisterLocalSettingRequest(
            installation_id=installation_id,
            raw_name=raw_name,
            menu_path=["图形", "自定义"] if menu_path is None else menu_path,
            identity_mode=identity_mode,
            reuse_local_setting_id=reuse_id,
            supersedes_local_setting_id=supersedes_id,
            raw_text=raw_text,
        ),
        _ctx(key, user),
    )


def _installation(service, repository, *, key: str = "install-base", label: str | None = None) -> str:
    game = _game()
    context = _public_context(game)
    _seed(repository, game, context)
    created = service.create_installation(
        CreateInstallationRequest(game_context_id=context.id, display_label=label),
        _ctx(key),
    )
    return installation_ids_of(created).pop()


def _definition_id(result) -> str:
    return next(ref.entity_id for ref in result.refs if ref.entity_type == "LocalSettingDefinition")


def _managed_file_id(result) -> str:
    return next(ref.entity_id for ref in result.refs if ref.entity_type == "ManagedFile")


def test_auto_registration_creates_one_located_definition(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)

    result = _register(service, installation_id, key="ls-1")

    assert _refs_types(result) == [
        "LocalSettingRegistration", "LocalSettingDefinition", "ManagedFile"
    ]
    choices = service.list_local_settings(installation_id, user_scope_id=USER)
    assert [choice.raw_name for choice in choices] == ["自定义设置甲"]
    assert choices[0].menu_path == ["图形", "自定义"]
    assert choices[0].location_known is True
    assert choices[0].definition_id == _definition_id(result)


def test_auto_reuses_the_exact_location_for_a_new_key(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)

    first = _register(service, installation_id, key="ls-1")
    second = _register(service, installation_id, key="ls-2")

    assert _definition_id(second) == _definition_id(first)
    assert len(service.list_local_settings(installation_id, user_scope_id=USER)) == 1
    assert _count(repository, "LocalSettingDefinition") == 1


def test_auto_same_key_concurrency_yields_one_definition(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)

    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def submit():
        try:
            barrier.wait(timeout=5)
            results.append(_register(service, installation_id, key="ls-race"))
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors
    assert len(results) == 2
    assert {_definition_id(item) for item in results} == {_definition_id(results[0])}
    assert _count(repository, "LocalSettingDefinition") == 1


def test_auto_requires_a_known_menu_path(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)

    with pytest.raises(ContractError) as caught:
        _register(service, installation_id, key="ls-unknown", menu_path=[], identity_mode="auto")
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "LocalSettingDefinition") == 0
    assert service.list_local_settings(installation_id, user_scope_id=USER) == []


def test_distinct_unknown_entries_stay_independent_series(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)

    first = _register(service, installation_id, key="ls-a", menu_path=[], identity_mode="distinct")
    second = _register(service, installation_id, key="ls-b", menu_path=[], identity_mode="distinct")

    assert _definition_id(first) != _definition_id(second)
    choices = service.list_local_settings(installation_id, user_scope_id=USER)
    assert len(choices) == 2
    assert all(choice.location_known is False for choice in choices)
    assert all(choice.menu_path == [] for choice in choices)


def test_reuse_requires_the_exact_current_definition(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)
    registered = _register(service, installation_id, key="ls-1")
    definition_id = _definition_id(registered)

    reused = _register(
        service,
        installation_id,
        key="ls-reuse",
        identity_mode="reuse",
        reuse_id=definition_id,
    )
    assert _definition_id(reused) == definition_id

    with pytest.raises(ContractError) as caught:
        _register(
            service,
            installation_id,
            key="ls-reuse-name",
            raw_name="另一个名字",
            identity_mode="reuse",
            reuse_id=definition_id,
        )
    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED


def test_cross_installation_and_stale_entries_are_refused(tmp_path):
    service, repository = _service(tmp_path)
    game = _game()
    context = _public_context(game)
    _seed(repository, game, context)
    first_install = installation_ids_of(
        service.create_installation(
            CreateInstallationRequest(game_context_id=context.id, display_label="one"), _ctx("i-1")
        )
    ).pop()
    second_install = installation_ids_of(
        service.create_installation(
            CreateInstallationRequest(game_context_id=context.id, display_label="two"), _ctx("i-2")
        )
    ).pop()
    definition_id = _definition_id(_register(service, first_install, key="ls-first"))

    with pytest.raises(ContractError) as caught:
        _register(
            service,
            second_install,
            key="ls-cross",
            identity_mode="reuse",
            reuse_id=definition_id,
        )
    assert caught.value.code is ErrorCode.REFERENCE_INVALID

    # A correction moves the head; reusing the old definition is now stale.
    corrected = _register(
        service,
        first_install,
        key="ls-correct",
        menu_path=["图形", "高级"],
        identity_mode="reuse",
        reuse_id=definition_id,
        supersedes_id=definition_id,
    )
    assert _definition_id(corrected) != definition_id
    with pytest.raises(ContractError) as caught:
        _register(
            service,
            first_install,
            key="ls-stale",
            identity_mode="reuse",
            reuse_id=definition_id,
        )
    assert caught.value.code is ErrorCode.LOCAL_SETTING_CONFLICT
    # The conflict points at the successor that now owns the series.
    assert caught.value.details.current_resource_ref is not None
    assert caught.value.details.current_resource_ref.entity_id == _definition_id(corrected)


def test_correction_creates_exactly_one_successor(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)
    original = _definition_id(_register(service, installation_id, key="ls-1"))

    corrected = _register(
        service,
        installation_id,
        key="ls-correct",
        raw_name="自定义设置乙",
        menu_path=["图形", "高级"],
        identity_mode="reuse",
        reuse_id=original,
        supersedes_id=original,
    )
    assert _definition_id(corrected) != original
    choices = service.list_local_settings(installation_id, user_scope_id=USER)
    assert [(c.raw_name, c.menu_path) for c in choices] == [("自定义设置乙", ["图形", "高级"])]

    with pytest.raises(ContractError) as caught:
        _register(
            service,
            installation_id,
            key="ls-correct-2",
            raw_name="另一个更正",
            menu_path=["图形", "高级"],
            identity_mode="reuse",
            reuse_id=original,
            supersedes_id=original,
        )
    assert caught.value.code is ErrorCode.LOCAL_SETTING_CONFLICT


def test_correction_without_metadata_change_returns_the_same_definition(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)
    original = _definition_id(_register(service, installation_id, key="ls-1"))

    same = _register(
        service,
        installation_id,
        key="ls-same",
        identity_mode="reuse",
        reuse_id=original,
        supersedes_id=original,
    )

    assert _definition_id(same) == original
    assert _count(repository, "LocalSettingDefinition") == 1


def test_correction_can_complete_an_unknown_location(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)
    original = _definition_id(
        _register(service, installation_id, key="ls-unknown", menu_path=[], identity_mode="distinct")
    )
    assert service.list_local_settings(installation_id, user_scope_id=USER)[0].location_known is False

    corrected = _register(
        service,
        installation_id,
        key="ls-locate",
        menu_path=["图形", "自定义"],
        identity_mode="reuse",
        reuse_id=original,
        supersedes_id=original,
    )

    assert _definition_id(corrected) != original
    choice = service.list_local_settings(installation_id, user_scope_id=USER)[0]
    assert choice.menu_path == ["图形", "自定义"]
    assert choice.location_known is True


def test_correction_can_clear_a_location_and_release_the_slot(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)
    original = _definition_id(_register(service, installation_id, key="ls-located"))

    cleared = _register(
        service,
        installation_id,
        key="ls-clear",
        menu_path=[],
        identity_mode="reuse",
        reuse_id=original,
        supersedes_id=original,
    )
    choice = service.list_local_settings(installation_id, user_scope_id=USER)[0]
    assert choice.definition_id == _definition_id(cleared)
    assert choice.menu_path == [] and choice.location_known is False

    # The old location is free again: a new automatic entry may claim it.
    reclaimed = _register(service, installation_id, key="ls-reclaim")
    assert _definition_id(reclaimed) not in {original, _definition_id(cleared)}
    assert len(service.list_local_settings(installation_id, user_scope_id=USER)) == 2


def test_local_setting_registration_manifest_records_the_target(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)

    result = _register(service, installation_id, key="ls-manifest", raw_text="记忆里的条目")
    file_id = _managed_file_id(result)
    stored = json.loads(service.files.read_managed_file(file_id))
    assert stored["installation_id"] == installation_id
    assert stored["definition_id"] == _definition_id(result)
    assert stored["namespace"] == "player"
    assert stored["request"]["raw_text"] == "记忆里的条目"

    replay = _register(service, installation_id, key="ls-manifest", raw_text="记忆里的条目")
    assert replay.model_dump() == result.model_dump()


def test_list_local_settings_is_read_only(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)
    _register(service, installation_id, key="ls-1")
    before = _count(repository, "LocalSettingDefinition")

    choices = service.list_local_settings(installation_id, user_scope_id=USER)

    assert isinstance(choices[0], LocalSettingChoice)
    assert _count(repository, "LocalSettingDefinition") == before


def test_same_key_with_new_content_conflicts_for_local_settings(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)
    _register(service, installation_id, key="ls-key")

    with pytest.raises(ContractError) as caught:
        _register(service, _uid(4242), key="ls-key")
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_impossible_identity_combinations_are_rejected(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)
    definition_id = _definition_id(_register(service, installation_id, key="ls-base"))

    cases = [
        # auto needs a located path
        dict(key="bad-1", menu_path=[], identity_mode="auto"),
        # auto never takes an explicit target
        dict(key="bad-2", identity_mode="auto", reuse_id=definition_id),
        # distinct is for an unknown position only
        dict(key="bad-3", identity_mode="distinct"),
        # reuse needs a selection id
        dict(key="bad-4", identity_mode="reuse"),
        # a correction must target the definition it reuses
        dict(
            key="bad-5",
            identity_mode="reuse",
            reuse_id=definition_id,
            supersedes_id=_uid(1234),
        ),
    ]
    for case in cases:
        with pytest.raises(ContractError) as caught:
            _register(service, installation_id, **case)
        assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert _count(repository, "LocalSettingDefinition") == 1


def test_correction_cannot_take_a_location_owned_by_another_series(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)
    located = _definition_id(_register(service, installation_id, key="ls-located"))
    unknown = _definition_id(
        _register(service, installation_id, key="ls-unknown", menu_path=[], identity_mode="distinct")
    )

    with pytest.raises(ContractError) as caught:
        _register(
            service,
            installation_id,
            key="ls-correct-conflict",
            menu_path=["图形", "自定义"],
            identity_mode="reuse",
            reuse_id=unknown,
            supersedes_id=unknown,
        )
    assert caught.value.code is ErrorCode.LOCAL_SETTING_CONFLICT
    # Nothing was half-written: both series keep their metadata.
    by_id = {choice.definition_id: choice for choice in service.list_local_settings(installation_id, user_scope_id=USER)}
    assert by_id[located].location_known is True
    assert by_id[unknown].location_known is False


def test_list_local_settings_hides_other_users_installations(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)

    with pytest.raises(ContractError) as caught:
        service.list_local_settings(installation_id, user_scope_id=OTHER_USER)
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_registration_for_another_users_installation_is_not_available(tmp_path):
    service, repository = _service(tmp_path)
    installation_id = _installation(service, repository)

    with pytest.raises(ContractError) as caught:
        _register(service, installation_id, key="ls-other-user", user=OTHER_USER)
    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_client_cannot_choose_the_namespace_or_an_unknown_identity_mode():
    payload = {
        "installation_id": "0" * 32,
        "raw_name": "x",
        "menu_path": [],
        "identity_mode": "distinct",
        "reuse_local_setting_id": None,
        "supersedes_local_setting_id": None,
        "raw_text": "",
    }
    with pytest.raises(ValidationError):
        RegisterLocalSettingRequest.model_validate({**payload, "namespace": "game"})
    with pytest.raises(ValidationError):
        RegisterLocalSettingRequest.model_validate({**payload, "identity_mode": "guess"})
