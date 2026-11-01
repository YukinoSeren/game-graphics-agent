"""ENG-T02: durable registration, a single execution slot, idempotency and resume.

All provider traffic goes to a counting stub; no network or real model is used.
"""

import threading

import pytest

from game_app.contracts.common import MutationContext
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService
from game_app.tasks import HandlerOutcome, TaskService
from tests.product.support.spies import SpyProvider


def _uid(value: int) -> str:
    return f"{value:032x}"


def _ctx(key: str = "key-1", user: str | None = None) -> MutationContext:
    return MutationContext(
        user_scope_id=user or _uid(90),
        idempotency_key=key,
        request_id=_uid(91),
    )


def _knowledge_input(game_context: str | None = None, *, model_calls: int = 2) -> dict:
    return {
        "kind": "knowledge",
        "game_context_id": game_context or _uid(1),
        "budget": {"max_model_calls": model_calls},
    }


def _service(repo, provider=None) -> TaskService:
    return TaskService(repo, provider=provider or SpyProvider())


def _task_row(repo, task_id: str):
    with repo.unit_of_work() as uow:
        return uow.query("SELECT * FROM tasks WHERE task_id=?", (task_id,))[0]


def _calls(repo, task_id: str):
    with repo.unit_of_work() as uow:
        return uow.query("SELECT * FROM external_calls WHERE task_id=? ORDER BY intent_saved_at", (task_id,))


def test_same_key_concurrent_registration_creates_one_task(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def submit():
        try:
            barrier.wait(timeout=5)
            results.append(service.register_task(_knowledge_input(), _ctx()))
        except BaseException as error:  # surfaced below
            errors.append(error)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len({accepted.task_id for accepted in results}) == 1
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 1
        assert uow.query("SELECT COUNT(*) AS n FROM task_inputs")[0]["n"] == 1
        assert uow.query("SELECT COUNT(*) AS n FROM task_events")[0]["n"] == 1
    assert service.provider.sends == 0


def test_same_key_different_payload_is_an_idempotency_conflict(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    service.register_task(_knowledge_input(), _ctx())
    with pytest.raises(ContractError) as caught:
        service.register_task(_knowledge_input(_uid(2)), _ctx())
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 1


def test_retry_after_commit_returns_original_acceptance_and_current_state(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    first = service.register_task(_knowledge_input(), _ctx())
    outcome = service.run_once()
    assert outcome is not None and outcome.state.value == "completed"

    retried = service.register_task(_knowledge_input(), _ctx())
    assert retried == first  # original acceptance, still queued in its own body
    view = service.get_task(first.task_id)
    assert view.task.state.value == "completed"
    assert service.provider.sends == 0


def test_registered_contract_version_is_bound_to_the_key(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    accepted = service.register_task(
        _knowledge_input(), _ctx(), request_schema_version="product-contract/0.3"
    )
    assert service.register_task(
        _knowledge_input(), _ctx(), request_schema_version="product-contract/0.3"
    ) == accepted
    with pytest.raises(ContractError) as caught:
        service.register_task(
            _knowledge_input(), _ctx(), request_schema_version="product-contract/0.4"
        )
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_tombstoned_key_returns_gone_and_never_reexecutes(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    accepted = service.register_task(_knowledge_input(), _ctx())
    with repo.unit_of_work() as uow:
        uow.tombstone_idempotency(
            _uid(90), "POST", "/api/knowledge-tasks", "key-1"
        )
    with pytest.raises(ContractError) as caught:
        service.register_task(_knowledge_input(), _ctx())
    assert caught.value.code is ErrorCode.RESOURCE_GONE
    assert service.get_task(accepted.task_id) is not None


def test_startup_interrupts_queued_and_running_but_keeps_waiting(repo_factory):
    repo = repo_factory()
    service = _service(repo)

    waiting = service.register_task(_knowledge_input(_uid(3)), _ctx("k-waiting"))

    def suspend(ctx):
        ctx.suspend(information_request_id=_uid(700))

    service.handlers["knowledge"] = suspend
    assert service.run_once().task_id == waiting.task_id
    assert service.get_task(waiting.task_id).task.state.value == "waiting_input"

    queued = service.register_task(_knowledge_input(), _ctx("k-queued"))
    running = service.register_task(_knowledge_input(_uid(2)), _ctx("k-running"))

    # Simulate a process exit while the second task was mid-flight.
    with repo.unit_of_work() as uow:
        uow.execute("UPDATE tasks SET state='running' WHERE task_id=?", (running.task_id,))
        uow.execute(
            "INSERT INTO external_calls(call_id, task_id, input_revision, kind, call_state,"
            " provider, input_hash, intent_saved_at, billing_state)"
            " VALUES(?, ?, 1, 'model', 'in_flight', 'stub', ?, ?, 'not_reported')",
            (_uid(800), running.task_id, "c" * 64, "2026-11-02T00:00:00+00:00"),
        )

    service.recover_on_startup()
    assert service.get_task(queued.task_id).task.state.value == "interrupted"
    assert service.get_task(running.task_id).task.state.value == "interrupted"
    assert service.get_task(waiting.task_id).task.state.value == "waiting_input"
    calls = _calls(repo, running.task_id)
    assert calls[0]["call_state"] == "outcome_unknown"


def test_waiting_input_releases_the_single_execution_slot(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    first = service.register_task(_knowledge_input(), _ctx("k1"))

    def suspend(ctx):
        ctx.suspend(information_request_id=_uid(701))

    service.handlers["knowledge"] = suspend
    assert service.run_once().state.value == "waiting_input"

    second = service.register_task(_knowledge_input(_uid(2)), _ctx("k2"))
    service.handlers["knowledge"] = lambda ctx: None
    outcome = service.run_once()
    assert outcome is not None and outcome.task_id == second.task_id
    assert outcome.state.value == "completed"
    assert service.run_once() is None
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks WHERE state='running'")[0]["n"] == 0


def test_cancel_queued_is_terminal_and_repeatable(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    accepted = service.register_task(_knowledge_input(), _ctx())
    result = service.cancel_task(accepted.task_id, _ctx("cancel-key"))
    assert [ref.entity_id for ref in result.refs] == [accepted.task_id]
    assert service.get_task(accepted.task_id).task.state.value == "cancelled"
    again = service.cancel_task(accepted.task_id, _ctx("cancel-key-2"))
    assert [ref.entity_id for ref in again.refs] == [accepted.task_id]
    assert service.get_task(accepted.task_id).task.state.value == "cancelled"
    assert service.run_once() is None


def test_cancel_accepted_before_completion_wins(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    accepted = service.register_task(_knowledge_input(), _ctx("k1"))
    entered = threading.Event()
    release = threading.Event()

    def slow(ctx):
        entered.set()
        release.wait(timeout=10)
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = slow
    outcomes: list[object] = []
    worker = threading.Thread(target=lambda: outcomes.append(service.run_once()))
    worker.start()
    assert entered.wait(timeout=5)
    service.cancel_task(accepted.task_id, _ctx("k-cancel"))
    release.set()
    worker.join(timeout=10)

    assert outcomes and outcomes[0].state.value == "cancelled"
    view = service.get_task(accepted.task_id)
    assert view.task.state.value == "cancelled"
    assert view.task.cancel_requested is True


def test_in_flight_and_budget_are_persisted_before_the_provider_call(repo_factory):
    repo = repo_factory()
    observed: dict[str, object] = {}

    def on_send(_request):
        with repo.unit_of_work() as uow:
            call = uow.query(
                "SELECT call_state FROM external_calls ORDER BY intent_saved_at DESC LIMIT 1"
            )[0]
            task = uow.query("SELECT calls FROM tasks LIMIT 1")[0]
        observed["call_state"] = call["call_state"]
        observed["calls"] = task["calls"]

    provider = SpyProvider(on_send=on_send)
    service = _service(repo, provider)

    def handler(ctx):
        ctx.invoke_provider(payload={"prompt": "synthetic"})
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    service.register_task(_knowledge_input(model_calls=1), _ctx())
    outcome = service.run_once()

    assert outcome.state.value == "completed"
    assert provider.sends == 1
    assert observed["call_state"] == "in_flight"
    assert '"model":1' in observed["calls"]


def test_provider_timeout_records_unknown_without_retry(repo_factory):
    repo = repo_factory()
    provider = SpyProvider(mode="unknown")
    service = _service(repo, provider)

    def handler(ctx):
        ctx.invoke_provider(payload={"prompt": "synthetic"})
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(model_calls=1), _ctx())
    outcome = service.run_once()

    assert outcome.state.value == "partial"
    view = service.get_task(accepted.task_id)
    assert view.task.last_error_code == "CALL_OUTCOME_UNKNOWN"
    calls = _calls(repo, accepted.task_id)
    assert [call["call_state"] for call in calls] == ["outcome_unknown"]
    assert service.run_once() is None
    assert provider.sends == 1
    with repo.unit_of_work() as uow:
        calls_json = uow.query("SELECT calls FROM tasks WHERE task_id=?", (accepted.task_id,))[0]["calls"]
    assert '"model":1' in calls_json


def test_resume_without_checkpoint_copies_input_and_creates_parent_link(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    parent = service.register_task(_knowledge_input(), _ctx("k1"))
    service.recover_on_startup()
    assert service.get_task(parent.task_id).task.state.value == "interrupted"

    resumed = service.resume_task(
        {"parent_task_id": parent.task_id, "checkpoint_id": None, "budget": {"max_model_calls": 1}},
        _ctx("k-resume"),
    )
    view = service.get_task(resumed.task_id)
    assert view.task.parent_id == parent.task_id
    service.handlers["knowledge"] = lambda ctx: HandlerOutcome.completed()
    assert service.run_once().state.value == "completed"


def test_resume_does_not_resend_when_parent_has_unknown_call(repo_factory):
    repo = repo_factory()
    provider = SpyProvider(mode="unknown")
    service = _service(repo, provider)

    def handler(ctx):
        ctx.invoke_provider(payload={"prompt": "synthetic"})

    service.handlers["knowledge"] = handler
    parent = service.register_task(_knowledge_input(model_calls=1), _ctx("k1"))
    assert service.run_once().state.value == "partial"
    assert provider.sends == 1

    resumed = service.resume_task(
        {"parent_task_id": parent.task_id, "checkpoint_id": None, "budget": {"max_model_calls": 2}},
        _ctx("k-resume"),
    )
    service.handlers["knowledge"] = lambda ctx: HandlerOutcome.completed()
    outcome = service.run_once()
    assert outcome.state.value == "partial"
    assert provider.sends == 1
    view = service.get_task(resumed.task_id)
    assert view.task.last_error_code == "CALL_OUTCOME_UNKNOWN"


def test_resume_rejects_foreign_checkpoint_and_hidden_own_checkpoint(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    parent = service.register_task(_knowledge_input(), _ctx("k1"))
    service.recover_on_startup()
    checkpoint = service.record_checkpoint(parent.task_id, ["acquire"])

    other = service.register_task(_knowledge_input(_uid(2)), _ctx("k2"))
    service.recover_on_startup()
    foreign = service.record_checkpoint(other.task_id, ["acquire"])

    with pytest.raises(ContractError) as caught:
        service.resume_task(
            {"parent_task_id": parent.task_id, "checkpoint_id": foreign, "budget": {}},
            _ctx("k-resume-foreign"),
        )
    assert caught.value.code is ErrorCode.REFERENCE_INVALID

    with pytest.raises(ContractError) as caught:
        service.resume_task(
            {"parent_task_id": parent.task_id, "checkpoint_id": None, "budget": {}},
            _ctx("k-resume-hidden"),
        )
    assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED

    # The valid path reuses the checkpoint and records its steps.
    resumed = service.resume_task(
        {"parent_task_id": parent.task_id, "checkpoint_id": checkpoint, "budget": {}},
        _ctx("k-resume-ok"),
    )
    assert resumed.task_id != parent.task_id
    assert service.get_task(resumed.task_id).progress.completed_steps == ["acquire"]


def test_running_task_cannot_be_resumed(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    parent = service.register_task(_knowledge_input(), _ctx("k1"))
    with repo.unit_of_work() as uow:
        uow.execute("UPDATE tasks SET state='running' WHERE task_id=?", (parent.task_id,))
    with pytest.raises(ContractError) as caught:
        service.resume_task(
            {"parent_task_id": parent.task_id, "checkpoint_id": None, "budget": {}},
            _ctx("k-resume"),
        )
    assert caught.value.code is ErrorCode.TASK_STATE_CONFLICT


def test_provider_response_is_published_as_a_managed_original(repo_factory, tmp_path):
    repo = repo_factory()
    files = FileService(repo, tmp_path / "managed")
    service = TaskService(repo, provider=SpyProvider(), files=files)

    def handler(ctx):
        ctx.invoke_provider(payload={"prompt": "synthetic"})
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(model_calls=1), _ctx())
    assert service.run_once().state.value == "completed"

    call = _calls(repo, accepted.task_id)[0]
    assert call["call_state"] == "succeeded"
    assert call["response_file_id"]
    original = files.read_managed_file(call["response_file_id"])
    assert b"stub-model-1" in original


def test_response_storage_failure_leaves_the_call_unknown(repo_factory, tmp_path, monkeypatch):
    repo = repo_factory()
    files = FileService(repo, tmp_path / "managed")
    service = TaskService(repo, provider=SpyProvider(), files=files)

    def storage_failure(*_args, **_kwargs):
        raise OSError("simulated response disk failure")

    monkeypatch.setattr(files, "store_artifact", storage_failure)

    def handler(ctx):
        ctx.invoke_provider(payload={"prompt": "synthetic"})
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(model_calls=1), _ctx())
    outcome = service.run_once()
    assert outcome.state.value == "partial"
    call = _calls(repo, accepted.task_id)[0]
    assert call["call_state"] == "outcome_unknown"
    assert call["response_file_id"] is None
    assert service.get_task(accepted.task_id).task.last_error_code == "CALL_OUTCOME_UNKNOWN"
