"""KNW-T01: public game identity, declaration slots and purpose-level reuse.

The catalog is local only: no network, no probing, no model calls.  The 01
GameContext builder is a real minimal implementation (DEV-T02 will extend it).
"""

import threading

import pytest

from game_app.contracts.common import CheckResult, EvidenceRef
from game_app.contracts.device import GameContext
from game_app.contracts.enums import (
    CheckStatus,
    CollectionResult,
    CoverageStatus,
    GameIdentityStatus,
    GapPriority,
    GapStatus,
    PublicationStatus,
)
from game_app.contracts.knowledge import (
    CoverageCell,
    Game,
    GameAlias,
    GameCatalogQuery,
    GameSelection,
    GameSelectionRequest,
    KnowledgeGap,
    KnowledgePackage,
)
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService, PublishHooks
from game_app.knowledge import IdentityService
from game_app.repository import ProductRepository

SCOPE = "collection.public_graphics"
OTHER_SCOPE = "recommendation.target"


def _uid(value: int) -> str:
    return f"{value:032x}"


def _now() -> str:
    return "2026-11-02T00:00:00+00:00"


def _check(status: CheckStatus = CheckStatus.pass_) -> CheckResult:
    return CheckResult(
        check_id="identity.evidence",
        definition_version="identity-checks/0.4",
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else _now(),
        method="synthetic test check",
    )


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        kind="source_location",
        source_revision_id=_uid(900),
        block_id="block-1",
        scope="section",
        quote="Official launcher entry names the game.",
        quote_hash="e" * 64,
    )


def _game(
    uid: int,
    series: int,
    name: str,
    *,
    version: int = 1,
    status: GameIdentityStatus = GameIdentityStatus.verified,
    platforms: tuple[str, ...] = ("steam",),
    declaring_user: str | None = None,
    aliases: tuple[GameAlias, ...] = (),
) -> Game:
    return Game(
        id=_uid(uid),
        schema_version="product-contract/0.4",
        created_at=_now(),
        created_by_task_id=None,
        series_id=_uid(series),
        version=version,
        canonical_name=name,
        aliases=list(aliases),
        platform_scope=list(platforms),
        identity_status=status,
        identity_checks=[_check()],
        declaring_user_scope_id=declaring_user,
    )


def _package(
    uid: int,
    *,
    game: Game,
    platforms: tuple[str, ...] = ("steam",),
    cells: tuple[CoverageCell, ...] = (),
    gaps: tuple[KnowledgeGap, ...] = (),
    status: PublicationStatus = PublicationStatus.published,
    version: int = 1,
) -> KnowledgePackage:
    return KnowledgePackage(
        id=_uid(uid),
        schema_version="product-contract/0.4",
        created_at=_now(),
        created_by_task_id=None,
        series_id=_uid(uid + 5000),
        version=version,
        game_id=game.series_id,
        game_revision_id=game.id,
        platform_scope=list(platforms),
        parent_package_id=None,
        import_snapshot_ids=[],
        source_revision_ids=[],
        record_snapshot_ids=[],
        setting_catalog=[],
        configuration_templates=[],
        measurement_evidence=[],
        constraints=[],
        diagnostic_record_ids=[],
        coverage_matrix=list(cells),
        critical_gaps=list(gaps),
        noncritical_gaps=[],
        checks=[_check()],
        relation_ids=[],
        collection_result=None,
        stop_reason=None,
        collection_execution_ref=None,
        publication_status=status,
        declared_scope=SCOPE,
        checked_at=None,
    )


def _cell(key: str, status: str, scope: str = SCOPE) -> CoverageCell:
    return CoverageCell(
        key=key,
        dimensions={},
        operation_scope=scope,
        status=CoverageStatus(status),
        evidence_refs=[_evidence()],
        note="synthetic coverage cell",
    )


def _gap(key: str, scope: str = SCOPE) -> KnowledgeGap:
    return KnowledgeGap(
        id=_uid(7000),
        legacy_gap_id=None,
        operation_scope=scope,
        priority=GapPriority.P1,
        question=f"missing {key}",
        record_refs=[],
        diagnostic_record_ids=[],
        status=GapStatus.open,
        stop_reason=None,
        evidence_refs=[],
        checks=[],
    )


@pytest.fixture
def identity(tmp_path) -> IdentityService:
    repo = ProductRepository(tmp_path / "product.sqlite3")
    repo.migrate()
    files = FileService(repo, tmp_path / "managed")
    service = IdentityService(repo, files)
    yield service
    repo.close()


def _request(raw_name: str = "示例游戏甲", **overrides) -> GameSelectionRequest:
    payload = {
        "raw_name": raw_name,
        "selected_game_revision_id": None,
        "platform": None,
        "installation_id": None,
        "expected_installation_revision": None,
    }
    payload.update(overrides)
    return GameSelectionRequest.model_validate(payload)


def _select(service: IdentityService, request: GameSelectionRequest, key: str, user: int = 90):
    return service.select_game(
        request,
        user_scope_id=_uid(user),
        idempotency_key=key,
        request_id=_uid(91),
    )


def _selection(service: IdentityService, selection_id: str) -> GameSelection:
    return service.get_selection(selection_id)


def _count(repo: ProductRepository, entity_type: str) -> int:
    with repo.unit_of_work() as uow:
        return uow.query(
            "SELECT COUNT(*) AS n FROM objects WHERE entity_type=?", (entity_type,)
        )[0]["n"]


def test_empty_catalog_returns_an_empty_page_without_writes(identity):
    page = identity.list_games(GameCatalogQuery(query="", cursor=None, limit=20))
    assert page.choices == []
    assert page.next_cursor is None
    assert _count(identity.repository, "Game") == 0
    assert _count(identity.repository, "GameSelection") == 0


def test_select_by_name_creates_provisional_identity_and_context(identity):
    result = _select(identity, _request(), "selection-key")

    kinds = [ref.entity_type for ref in result.refs]
    assert kinds == ["GameSelection", "Game", "GameContext"]

    selection = _selection(identity, result.refs[0].entity_id)
    assert selection.state == "selected"
    assert selection.game_revision_id == result.refs[1].entity_id
    assert selection.game_context_id == result.refs[2].entity_id

    game = identity.repository.get_object("Game", result.refs[1].entity_id)
    assert game.payload["canonical_name"] == "示例游戏甲"
    assert game.payload["identity_status"] == "provisional"
    assert game.payload["platform_scope"] == []
    assert game.payload["declaring_user_scope_id"] == _uid(90)

    stored_context = identity.repository.get_object("GameContext", result.refs[2].entity_id)
    context = GameContext.model_validate_json(stored_context.payload_bytes)
    assert context.game_id == game.payload["series_id"]
    assert context.game_revision_id == game.object_id
    assert context.installation_id is None
    assert context.platform.value_state.value == "unknown"
    assert context.adapter_id is None and context.adapter_version is None
    assert context.capabilities.can_read_config.status is CheckStatus.not_run
    assert identity.provider_sends == 0


def test_same_key_replays_the_original_selection(identity):
    first = _select(identity, _request(), "selection-key")
    second = _select(identity, _request(), "selection-key")
    assert second.refs == first.refs
    assert _count(identity.repository, "GameSelection") == 1
    assert _count(identity.repository, "Game") == 1


def test_same_key_with_different_content_conflicts(identity):
    _select(identity, _request(), "selection-key")
    with pytest.raises(ContractError) as caught:
        _select(identity, _request("另一个游戏"), "selection-key")
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert _count(identity.repository, "GameSelection") == 1


def test_declaration_slot_is_shared_across_keys_and_name_variants(identity):
    first = _select(identity, _request("示例 游戏甲"), "key-1")
    second = _select(identity, _request("  示例 游戏甲  "), "key-2")

    first_game = _selection(identity, first.refs[0].entity_id).game_revision_id
    second_game = _selection(identity, second.refs[0].entity_id).game_revision_id
    assert first_game == second_game
    assert _count(identity.repository, "Game") == 1
    assert _count(identity.repository, "GameSelection") == 2


def test_concurrent_selections_of_one_slot_create_one_series(identity):
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def submit(key: str):
        try:
            barrier.wait(timeout=5)
            results.append(_select(identity, _request("并发游戏"), key))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=submit, args=(f"key-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    series = {
        identity.repository.get_object("Game", result.refs[1].entity_id).payload["series_id"]
        for result in results
    }
    assert len(series) == 1
    assert _count(identity.repository, "Game") == 1
    assert _count(identity.repository, "GameSelection") == 2


def test_two_verified_candidates_require_an_explicit_choice(identity):
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", _game(1, 101, "同名游戏"))
        uow.insert_versioned("Game", _game(2, 102, "同名游戏", platforms=("epic",)))

    result = _select(identity, _request("同名游戏"), "selection-key")
    assert [ref.entity_type for ref in result.refs] == ["GameSelection"]

    selection = _selection(identity, result.refs[0].entity_id)
    assert selection.state == "needs_choice"
    assert selection.game_revision_id is None
    assert selection.game_context_id is None
    assert set(selection.candidate_revision_ids) == {_uid(1), _uid(2)}
    assert _count(identity.repository, "GameContext") == 0


def test_explicit_revision_selection_uses_that_candidate(identity):
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", _game(1, 101, "同名游戏"))
        uow.insert_versioned("Game", _game(2, 102, "同名游戏", platforms=("epic",)))

    result = _select(
        identity, _request("同名游戏", selected_game_revision_id=_uid(2)), "selection-key"
    )
    assert [ref.entity_type for ref in result.refs] == ["GameSelection", "Game", "GameContext"]
    assert result.refs[1].entity_id == _uid(2)
    assert _selection(identity, result.refs[0].entity_id).state == "selected"


def test_another_users_provisional_game_is_not_visible_or_reused(identity):
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned(
            "Game",
            _game(
                1, 101, "别人登记的游戏",
                status=GameIdentityStatus.provisional,
                platforms=(),
                declaring_user=_uid(77),
            ),
        )

    page = identity.list_games(GameCatalogQuery(query="", cursor=None, limit=20))
    assert page.choices == []

    result = _select(identity, _request("别人登记的游戏"), "selection-key")
    game = identity.repository.get_object("Game", result.refs[1].entity_id)
    assert game.payload["declaring_user_scope_id"] == _uid(90)
    assert game.payload["series_id"] != _uid(101)


def test_declared_platform_stays_provisional(identity):
    result = _select(identity, _request("带平台的游戏", platform="steam"), "selection-key")
    game = identity.repository.get_object("Game", result.refs[1].entity_id).payload
    assert game["platform_scope"] == ["steam"]
    assert game["identity_status"] == "provisional"
    assert all(check["status"] != "pass" for check in game["identity_checks"])


def test_crash_after_rename_before_commit_leaves_no_selection(identity):
    from tests.product.test_files import SimulatedCrash

    def crash(_path):
        raise SimulatedCrash("process exited after rename")

    with pytest.raises(SimulatedCrash):
        identity.select_game(
            _request(),
            user_scope_id=_uid(90),
            idempotency_key="selection-key",
            request_id=_uid(91),
            hooks=PublishHooks(after_rename=crash),
        )

    assert _count(identity.repository, "GameSelection") == 0
    assert _count(identity.repository, "Game") == 0
    assert _count(identity.repository, "GameContext") == 0

    retried = _select(identity, _request(), "selection-key")
    assert [ref.entity_type for ref in retried.refs] == ["GameSelection", "Game", "GameContext"]
    assert _count(identity.repository, "GameSelection") == 1


def test_unverified_alias_is_not_used_for_automatic_matching(identity):
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", _game(
            1, 101, "正式名称",
            aliases=(GameAlias(
                name="旧译名",
                platform=None,
                evidence_refs=[_evidence()],
                status=CheckStatus.unknown,
            ),),
        ))

    result = _select(identity, _request("旧译名"), "selection-key")
    selection = _selection(identity, result.refs[0].entity_id)
    assert selection.state == "selected"
    # A provisional identity is created; the unverified alias never merges.
    assert result.refs[1].entity_id != _uid(1)
    assert selection.game_revision_id == result.refs[1].entity_id


def test_verified_alias_matches_the_existing_series(identity):
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", _game(
            1, 101, "正式名称",
            aliases=(GameAlias(
                name="旧译名",
                platform="steam",
                evidence_refs=[_evidence()],
                status=CheckStatus.pass_,
            ),),
        ))

    result = _select(identity, _request("旧译名", platform="steam"), "selection-key")
    assert result.refs[1].entity_id == _uid(1)
    assert _count(identity.repository, "Game") == 1


def test_resolve_game_identity_adds_a_verified_version_in_the_same_series(identity):
    selected = _select(identity, _request(), "selection-key")
    provisional = identity.repository.get_object("Game", selected.refs[1].entity_id).payload

    resolution = identity.resolve_game_identity(
        series_id=provisional["series_id"],
        platform_scope=["steam"],
        evidence_refs=[_evidence()],
        checks=[_check()],
    )
    assert resolution.game_ref is not None
    verified = identity.repository.get_object("Game", resolution.game_ref.entity_id).payload
    assert verified["series_id"] == provisional["series_id"]
    assert verified["version"] == 2
    assert verified["identity_status"] == "verified"
    assert verified["declaring_user_scope_id"] is None
    # The earlier provisional version is untouched.
    assert identity.repository.get_object("Game", selected.refs[1].entity_id).payload[
        "identity_status"
    ] == "provisional"


def test_resolve_game_identity_refuses_without_passing_evidence(identity):
    selected = _select(identity, _request(), "selection-key")
    series = identity.repository.get_object("Game", selected.refs[1].entity_id).payload["series_id"]

    resolution = identity.resolve_game_identity(
        series_id=series,
        platform_scope=["steam"],
        evidence_refs=[_evidence()],
        checks=[_check(CheckStatus.unknown)],
    )
    assert resolution.game_ref is None
    assert resolution.reason
    assert _count(identity.repository, "Game") == 1


def test_assess_reuse_discovers_without_any_package(identity):
    selected = _select(identity, _request(), "selection-key")
    decision = identity.assess_reuse(
        game_context_id=selected.refs[2].entity_id,
        device_snapshot_id=None,
        package_id=None,
        operation_scope=SCOPE,
    )
    assert decision.mode == "discover"
    assert decision.package_id is None
    assert decision.reason


def test_assess_reuse_rejects_a_package_for_another_platform(identity):
    game = _game(1, 101, "有包的游戏")
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
        uow.insert_versioned("KnowledgePackage", _package(
            2, game=game, platforms=("epic",),
            cells=(_cell("settings.menu", "covered"),),
        ))
    selected = _select(
        identity, _request("有包的游戏", selected_game_revision_id=game.id, platform="steam"),
        "selection-key",
    )

    decision = identity.assess_reuse(
        game_context_id=selected.refs[2].entity_id,
        device_snapshot_id=None,
        package_id=None,
        operation_scope=SCOPE,
    )
    assert decision.mode == "discover"
    assert any(check.status is CheckStatus.fail for check in decision.checks)


def test_assess_reuse_follows_up_when_scope_coverage_is_missing(identity):
    game = _game(1, 101, "部分覆盖的游戏")
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
        uow.insert_versioned("KnowledgePackage", _package(
            2, game=game,
            cells=(_cell("settings.menu", "covered"), _cell("settings.quality", "unknown")),
            gaps=(_gap("settings.quality"),),
        ))
    selected = _select(
        identity, _request("部分覆盖的游戏", selected_game_revision_id=game.id), "selection-key"
    )

    decision = identity.assess_reuse(
        game_context_id=selected.refs[2].entity_id,
        device_snapshot_id=None,
        package_id=None,
        operation_scope=SCOPE,
    )
    assert decision.mode == "followup"
    assert decision.package_id == _uid(2)
    assert "settings.quality" in decision.gap_keys


def test_assess_reuse_reuses_a_fully_covered_package(identity):
    game = _game(1, 101, "完整覆盖的游戏")
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
        uow.insert_versioned("KnowledgePackage", _package(
            2, game=game,
            cells=(_cell("settings.menu", "covered"), _cell("settings.quality", "not_applicable")),
        ))
    selected = _select(
        identity, _request("完整覆盖的游戏", selected_game_revision_id=game.id), "selection-key"
    )

    decision = identity.assess_reuse(
        game_context_id=selected.refs[2].entity_id,
        device_snapshot_id=None,
        package_id=None,
        operation_scope=SCOPE,
    )
    assert decision.mode == "reuse"
    assert decision.package_id == _uid(2)
    assert decision.gap_keys == []


def test_assess_reuse_ignores_cells_for_other_operation_scopes(identity):
    game = _game(1, 101, "多用途的游戏")
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
        uow.insert_versioned("KnowledgePackage", _package(
            2, game=game,
            cells=(_cell("settings.menu", "covered"), _cell("target.fps", "unknown", OTHER_SCOPE)),
        ))
    selected = _select(
        identity, _request("多用途的游戏", selected_game_revision_id=game.id), "selection-key"
    )

    decision = identity.assess_reuse(
        game_context_id=selected.refs[2].entity_id,
        device_snapshot_id=None,
        package_id=None,
        operation_scope=SCOPE,
    )
    assert decision.mode == "reuse"


def test_unpublished_package_is_not_reused(identity):
    game = _game(1, 101, "未发布的游戏")
    with identity.repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
        uow.insert_versioned("KnowledgePackage", _package(
            2, game=game, cells=(_cell("settings.menu", "covered"),),
            status=PublicationStatus.validated,
        ))
    selected = _select(
        identity, _request("未发布的游戏", selected_game_revision_id=game.id), "selection-key"
    )
    decision = identity.assess_reuse(
        game_context_id=selected.refs[2].entity_id,
        device_snapshot_id=None,
        package_id=None,
        operation_scope=SCOPE,
    )
    assert decision.mode == "discover"


def test_collection_result_is_not_inferred_from_coverage(identity):
    # A pure aggregate package has no single collection execution: all three
    # collection fields stay null even though coverage is complete.
    game = _game(1, 101, "聚合包游戏")
    package = _package(2, game=game, cells=(_cell("settings.menu", "covered"),))
    assert package.collection_result is None
    assert package.stop_reason is None
    assert package.collection_execution_ref is None
    assert CollectionResult.completed_scope.value == "completed_scope"
