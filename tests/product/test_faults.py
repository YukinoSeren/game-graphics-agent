"""VAL-T02 fault windows: crashes, lost responses, concurrency and tampering.

Every injected fault runs against a temporary repository; the injector refuses
``runs/`` and any path outside its guard root.  The provider stub counts sends
so a local retry cannot be mistaken for a re-execution.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from game_app.application import Application
from game_app.contracts.common import Budget, MutationContext
from game_app.contracts.performance import CaptureArtifact, CreateSessionRequest
from game_app.contracts.tasks import KnowledgeInput, ResumeTaskInput
from game_app.errors import ContractError, ErrorCode
from game_app.files import PublishHooks
from game_app.importer import FreezeHooks, FrozenInputReader
from game_app.repository import ProductRepository, canonical_json
from game_app.tasks import HandlerOutcome, TaskService
from tests.product.support.configuration import context_key, setup
from tests.product.support.performance import (
    analysis_input,
    artifact_from_csv,
    count_objects,
    csv_bytes,
    load_object,
    run_analysis,
    wired,
)
from tests.product.support.spies import CrashInjector, InjectedCrash, SpyProvider
from tests.product.test_sessions import FakeClock


# 08 §7's six boundaries and the case that injects each one.  The map is kept
# explicit so a missing boundary is visible rather than implied by file length.
FAULT_BOUNDARIES = {
    "register before/after, before the response":
        "test_same_key_concurrent_registration_creates_one_task",
    "external in_flight, before the response is stored":
        "test_a_lost_response_then_same_key_retry_does_not_resend",
    "file renamed, before the database commit":
        "test_an_import_freeze_crash_before_the_commit_leaves_no_half_import",
    "current pointer / answer / binding concurrency":
        "test_pointer_concurrency_lets_exactly_one_writer",
    "exit before the first checkpoint":
        "test_no_checkpoint_recovery_creates_one_child_and_does_not_resend",
    "corruption / migration failure":
        "test_a_failed_migration_is_rolled_back",
}


def test_every_fault_boundary_has_an_injected_case():
    missing = [
        boundary for boundary, name in FAULT_BOUNDARIES.items()
        if not callable(globals().get(name))
    ]
    assert not missing


def _ctx(key: str) -> MutationContext:
    return context_key(key)


def _session_request(installation, context, device) -> CreateSessionRequest:
    return CreateSessionRequest.model_validate_json(canonical_json({
        "installation_id": installation,
        "device_snapshot_id": device,
        "game_context_id": context,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation,
            "fields": {},
            "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }))


def _app(tmp_path):
    repository, files, installation, context, device = setup(tmp_path)
    clock = FakeClock()
    application = Application(
        repository, file_root=str(tmp_path / "managed"), clock=clock,
    )
    return repository, application, installation, context, device, clock


def _assert_no_partial_facts(repository) -> None:
    assert count_objects(repository, "PlaySession") == 0
    assert count_objects(repository, "SessionSegment") == 0
    assert count_objects(repository, "ConfigurationBinding") == 0
    assert count_objects(repository, "PlayerDeclaration") == 0
    assert count_objects(repository, "ManagedFile") == 0
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM idempotency")[0]["n"] == 0


@pytest.mark.parametrize("boundary", ["before_write", "after_rename", "before_commit"])
def test_a_crash_at_the_publishing_boundary_leaves_no_partial_facts(
    tmp_path, boundary
):
    repository, application, installation, context, device, clock = _app(tmp_path)
    injector = CrashInjector(boundary, guard_root=tmp_path)
    injector.guard(repository.path)
    hooks = PublishHooks(
        before_write=injector.hook("before_write"),
        after_rename=injector.hook("after_rename"),
        before_commit=injector.hook("before_commit"),
    )
    request = _session_request(installation, context, device)

    with pytest.raises(InjectedCrash):
        application.create_session(request, _ctx("crash"), hooks=hooks)

    assert injector.fired == 1
    _assert_no_partial_facts(repository)
    with repository.unit_of_work() as uow:
        phase = uow.query("SELECT phase FROM file_operations")[0]["phase"]
    assert phase in {"prepared", "files_ready"}

    # The same key and content retries the frozen file instead of rebuilding it.
    result = application.create_session(request, _ctx("crash"))
    assert result.refs
    assert count_objects(repository, "PlaySession") == 1
    assert count_objects(repository, "ManagedFile") == 1
    assert count_objects(repository, "PlayerDeclaration") == 1
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM idempotency")[0]["n"] == 1


def test_a_lost_response_then_same_key_retry_does_not_resend(tmp_path):
    repository, files, installation, context, device = setup(tmp_path)
    provider = SpyProvider(mode="lost")
    service = TaskService(repository, provider=provider, files=files, clock=FakeClock())

    def handler(task):
        task.invoke_provider(
            kind="model",
            payload={"prompt": "synthetic"},
            requested_model="stub-model",
            prompt_version="prompt/1",
            output_schema_version="schema/1",
        )
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    task_input = KnowledgeInput(
        kind="knowledge",
        game_context_id="c" * 32,
        device_snapshot_id=None,
        prior_package_id=None,
        budget=Budget(),
    )

    accepted = service.register_task(task_input, _ctx("lost"))
    outcome = service.run_once()

    assert outcome.state.value == "partial"
    assert provider.sends == 1 and provider.executed is True
    view = service.get_task(accepted.task_id)
    assert view.task.last_error_code == ErrorCode.CALL_OUTCOME_UNKNOWN.value

    replay = service.register_task(task_input, _ctx("lost"))
    assert replay.task_id == accepted.task_id
    assert provider.sends == 1
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 1


def test_same_key_concurrent_registration_creates_one_task(tmp_path):
    repository, files, installation, context, device = setup(tmp_path)
    service = TaskService(repository, provider=SpyProvider(), files=files, clock=FakeClock())
    task_input = KnowledgeInput(
        kind="knowledge",
        game_context_id="c" * 32,
        device_snapshot_id=None,
        prior_package_id=None,
        budget=Budget(),
    )
    barrier = threading.Barrier(2)

    def submit():
        barrier.wait(timeout=10)
        return service.register_task(task_input, _ctx("concurrent"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(submit) for _ in range(2)]
        accepted = [future.result(timeout=20) for future in results]

    assert accepted[0].task_id == accepted[1].task_id
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 1
        assert uow.query("SELECT COUNT(*) AS n FROM idempotency")[0]["n"] == 1


def test_a_tampered_original_is_unavailable_and_not_consumed(tmp_path):
    repository, files, service, tasks, installation, context, device, clock = wired(tmp_path)
    artifact, binding = artifact_from_csv(
        service, files, installation, context, device, csv_bytes([10, 20, 30, 40]), "tamper",
    )
    original = load_object(service, "CaptureArtifact", artifact.id, CaptureArtifact)
    _, view = run_analysis(
        service, tasks, analysis_input(artifact.id, binding), key="tamper-ok",
    )
    assert view.task.state.value == "completed"
    summaries = count_objects(repository, "PerformanceSummary")

    path = files.root / "objects" / original.file_id
    path.write_bytes(b"tampered bytes that no longer match the manifest")

    with pytest.raises(ContractError) as tampered:
        service.analyze(
            analysis_input(artifact.id, binding), _ctx("tamper-again")
        )
    assert tampered.value.code in {
        ErrorCode.ARTIFACT_UNAVAILABLE, ErrorCode.PRECONDITION_UNVERIFIED,
    }
    assert count_objects(repository, "PerformanceSummary") == summaries
    assert repository.find_idempotency(
        "a" * 32, "POST", "/api/analyses", "tamper-again"
    ) is None


def test_no_checkpoint_recovery_creates_one_child_and_does_not_resend(tmp_path):
    repository, files, installation, context, device = setup(tmp_path)
    provider = SpyProvider(mode="lost")
    service = TaskService(repository, provider=provider, files=files, clock=FakeClock())

    def handler(task):
        task.invoke_provider(
            kind="model",
            payload={"prompt": "synthetic"},
            requested_model="stub-model",
            prompt_version="prompt/1",
            output_schema_version="schema/1",
        )
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    task_input = KnowledgeInput(
        kind="knowledge",
        game_context_id="c" * 32,
        device_snapshot_id=None,
        prior_package_id=None,
        budget=Budget(),
    )
    parent = service.register_task(task_input, _ctx("parent"))
    service.run_once()
    assert provider.sends == 1

    child = service.resume_task(
        ResumeTaskInput(
            parent_task_id=parent.task_id, checkpoint_id=None, budget=Budget(),
        ),
        _ctx("child"),
    )
    outcome = service.run_once()

    assert outcome.state.value == "partial"
    assert provider.sends == 1
    assert child.task_id != parent.task_id
    with repository.unit_of_work() as uow:
        rows = uow.query(
            "SELECT task_id, parent_id, state FROM tasks ORDER BY created_at, rowid"
        )
    assert len(rows) == 2
    assert rows[1]["parent_id"] == parent.task_id
    assert rows[0]["state"] == "partial"
    assert rows[1]["state"] == "partial"


def test_the_crash_injector_refuses_a_non_temporary_repository(tmp_path):
    injector = CrashInjector("before_commit", guard_root=tmp_path)
    injector.guard(tmp_path / "product.sqlite3")
    with pytest.raises(InjectedCrash):
        injector.guard(Path("runs") / "player.sqlite3")
    with pytest.raises(InjectedCrash):
        injector.guard(tmp_path.parent / "elsewhere.sqlite3")
    with pytest.raises(InjectedCrash):
        CrashInjector("before_commit").guard(tmp_path / "product.sqlite3")


# 08 §7, "文件最终改名后、库发布前": the frozen copy may exist, but no import
# identity may be readable from a crash before the transaction commits.
def test_an_import_freeze_crash_before_the_commit_leaves_no_half_import(tmp_path):
    from tests.product.test_import_publish import _run_files

    repository, files, installation, context, device = setup(tmp_path)
    directory = tmp_path / "run"
    directory.mkdir()
    for name, data in _run_files().items():
        (directory / name).write_bytes(data)
    reader = FrozenInputReader(files)

    class Crash(Exception):
        pass

    with pytest.raises(Crash):
        reader.freeze(
            directory,
            user_scope_id="a" * 32,
            idempotency_key="freeze-crash",
            hooks=FreezeHooks(after_copy=lambda: (_ for _ in ()).throw(Crash())),
        )

    assert count_objects(repository, "ImportedRecord") == 0
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM import_snapshots")[0]["n"] == 0
        assert uow.query("SELECT COUNT(*) AS n FROM file_operations")[0]["n"] == 1

    # The same key retries against the frozen operation instead of rebuilding it.
    frozen = reader.freeze(
        directory, user_scope_id="a" * 32, idempotency_key="freeze-crash"
    )
    assert frozen.format_id == "collector-run/v0.1"
    assert count_objects(repository, "ManagedFile") == 5


# 08 §7, "当前指针/回答/绑定并发": one writer commits, the other gets a whole
# conflict, and no answer or revision is lost.
def test_pointer_concurrency_lets_exactly_one_writer(tmp_path):
    from tests.product.test_sessions import (
        test_concurrent_end_segment_lets_exactly_one_write,
    )

    directory = tmp_path / "concurrency"
    directory.mkdir()
    test_concurrent_end_segment_lets_exactly_one_write(directory)


# 08 §7, "损坏/迁移失败": a failed migration is rolled back and reports a
# storage error rather than an empty usable database.
def test_a_failed_migration_is_rolled_back(tmp_path, monkeypatch):
    import game_app.repository as repository_module

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_broken.sql").write_text(
        "CREATE TABLE broken (id TEXT);\n"
        "INSERT INTO a_table_that_does_not_exist(id) VALUES('x');\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repository_module, "MIGRATIONS_DIR", migrations)

    repository = ProductRepository(tmp_path / "product.sqlite3")
    with pytest.raises(ContractError) as caught:
        repository.migrate()
    assert caught.value.code is ErrorCode.STORAGE_COMMIT_FAILED

    with repository.unit_of_work() as uow:
        applied = uow.query("SELECT version FROM schema_migrations ORDER BY version")
    assert applied == []
    # The half-created table was rolled back with the rest of the script.
    with repository.unit_of_work() as uow:
        tables = {
            row["name"] for row in uow.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "broken" not in tables
