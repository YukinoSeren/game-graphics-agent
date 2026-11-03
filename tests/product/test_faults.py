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
from game_app.repository import canonical_json
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
