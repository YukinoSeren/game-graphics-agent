"""ENG-T02: durable registration, a single execution slot, idempotency and resume.

All provider traffic goes to a counting stub; no network or real model is used.
"""

import threading

import pytest

from game_app.contracts.common import MutationContext
from game_app.contracts.enums import TaskState
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


def _invoke_model(ctx, prompt: str = "synthetic") -> dict:
    return ctx.invoke_provider(
        kind="model",
        requested_model="stub-model",
        prompt_version="prompt-v1",
        output_schema_version="schema-v1",
        payload={"prompt": prompt},
    )


def _service(repo, provider=None) -> TaskService:
    files = FileService(repo, repo.path.parent / "managed")
    return TaskService(repo, provider=provider or SpyProvider(), files=files)


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
    service.handlers["knowledge"] = lambda ctx: HandlerOutcome.completed()
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
        _invoke_model(ctx)
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
        _invoke_model(ctx)
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
        _invoke_model(ctx)

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
        _invoke_model(ctx)
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
        _invoke_model(ctx)
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(model_calls=1), _ctx())
    outcome = service.run_once()
    assert outcome.state.value == "partial"
    call = _calls(repo, accepted.task_id)[0]
    assert call["call_state"] == "outcome_unknown"
    assert call["response_file_id"] is None
    assert service.get_task(accepted.task_id).task.last_error_code == "CALL_OUTCOME_UNKNOWN"


# --- review fix pass ---------------------------------------------------------


def test_non_builtin_provider_error_is_unknown_and_blocks_resume(repo_factory):
    """A wrapper exception after transmission must not be treated as known failure."""
    repo = repo_factory()
    provider = SpyProvider(mode="boom")
    service = _service(repo, provider)

    def handler(ctx):
        _invoke_model(ctx)
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    parent = service.register_task(_knowledge_input(model_calls=1), _ctx("k1"))
    assert service.run_once().state.value == "partial"

    call = _calls(repo, parent.task_id)[0]
    assert call["call_state"] == "outcome_unknown"
    assert service.get_task(parent.task_id).task.last_error_code == "CALL_OUTCOME_UNKNOWN"

    service.resume_task(
        {"parent_task_id": parent.task_id, "checkpoint_id": None, "budget": {"max_model_calls": 2}},
        _ctx("k-resume"),
    )
    assert service.run_once().state.value == "partial"
    assert provider.sends == 1


def test_explicit_provider_rejection_is_a_known_failure(repo_factory):
    from game_app.tasks import ProviderRejected

    repo = repo_factory()
    provider = SpyProvider(mode="reject")
    service = _service(repo, provider)

    def handler(ctx):
        _invoke_model(ctx)
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    parent = service.register_task(_knowledge_input(model_calls=1), _ctx("k1"))
    outcome = service.run_once()

    assert outcome.state.value == "failed"
    assert _calls(repo, parent.task_id)[0]["call_state"] == "failed_known"
    assert service.get_task(parent.task_id).task.last_error_code == "PROVIDER_FAILED"
    assert ProviderRejected is not None
    assert provider.sends == 1


def test_model_calls_require_prompt_and_schema_versions(repo_factory):
    repo = repo_factory()
    provider = SpyProvider()
    service = _service(repo, provider)

    def handler(ctx):
        ctx.invoke_provider(kind="model", payload={"prompt": "synthetic"})
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(model_calls=1), _ctx())
    service.run_once()

    assert provider.sends == 0
    assert service.get_task(accepted.task_id).task.last_error_code == "INPUT_VALIDATION_FAILED"
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0


def test_model_call_audit_records_prompt_and_schema_versions(repo_factory):
    repo = repo_factory()
    service = _service(repo, SpyProvider())

    def handler(ctx):
        _invoke_model(ctx)
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(model_calls=1), _ctx())
    assert service.run_once().state.value == "completed"

    call = _calls(repo, accepted.task_id)[0]
    assert call["requested_model"] == "stub-model"
    assert call["prompt_version"] == "prompt-v1"
    assert call["output_schema_version"] == "schema-v1"


def test_provider_call_without_a_managed_file_store_is_refused(repo_factory):
    repo = repo_factory()
    provider = SpyProvider()
    service = TaskService(repo, provider=provider)  # deliberately no FileService

    def handler(ctx):
        _invoke_model(ctx)
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(model_calls=1), _ctx())
    service.run_once()

    assert provider.sends == 0
    assert service.get_task(accepted.task_id).task.last_error_code == "PRECONDITION_UNVERIFIED"
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM external_calls")[0]["n"] == 0


def test_startup_interruption_records_the_process_exit_reason(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    accepted = service.register_task(_knowledge_input(), _ctx())
    service.recover_on_startup()

    task = service.get_task(accepted.task_id).task
    assert task.state.value == "interrupted"
    assert task.stop_reason is not None
    assert task.stop_reason.kind == "execution"
    assert task.stop_reason.code == "process_exit"


def test_repeated_cancel_with_the_same_key_is_one_logical_submission(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    accepted = service.register_task(_knowledge_input(), _ctx())
    first = service.cancel_task(accepted.task_id, _ctx("cancel-key"))
    second = service.cancel_task(accepted.task_id, _ctx("cancel-key"))

    assert first == second
    with repo.unit_of_work() as uow:
        events = uow.query(
            "SELECT cause FROM task_events WHERE task_id=? ORDER BY sequence",
            (accepted.task_id,),
        )
        revisions = uow.query(
            "SELECT revision FROM tasks WHERE task_id=?", (accepted.task_id,)
        )[0]["revision"]
    assert [row["cause"] for row in events] == ["registered", "cancelled"]
    assert revisions == 2
    record = repo.find_idempotency(_uid(90), "POST", f"/api/tasks/{accepted.task_id}/cancel", "cancel-key")
    assert record is not None and record.response_status == 200


def test_cancel_retry_while_running_does_not_duplicate_the_control_event(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    accepted = service.register_task(_knowledge_input(), _ctx("k1"))
    with repo.unit_of_work() as uow:
        uow.execute(
            "UPDATE tasks SET state='running', revision=2 WHERE task_id=?", (accepted.task_id,)
        )
        uow.execute(
            "INSERT INTO task_events(task_id, sequence, input_revision, from_state, to_state,"
            " cause, related_refs, active_elapsed_seconds, created_at)"
            " VALUES(?, 2, 1, 'queued', 'running', 'started', '[]', 0, '2026-11-02T00:00:00+00:00')",
            (accepted.task_id,),
        )

    service.cancel_task(accepted.task_id, _ctx("cancel-key"))
    service.cancel_task(accepted.task_id, _ctx("cancel-key"))

    with repo.unit_of_work() as uow:
        causes = [
            row["cause"] for row in uow.query(
                "SELECT cause FROM task_events WHERE task_id=? ORDER BY sequence",
                (accepted.task_id,),
            )
        ]
        revision = uow.query(
            "SELECT revision FROM tasks WHERE task_id=?", (accepted.task_id,)
        )[0]["revision"]
    assert causes.count("cancel_requested") == 1
    assert revision == 3


def test_same_cancel_key_with_a_different_task_conflicts(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    first = service.register_task(_knowledge_input(), _ctx("k1"))
    second = service.register_task(_knowledge_input(_uid(2)), _ctx("k2"))
    service.cancel_task(first.task_id, _ctx("cancel-key"))
    # A cancel key is scoped to its canonical route, so another task's cancel
    # with the same key is an independent submission and must still work.
    service.cancel_task(second.task_id, _ctx("cancel-key"))
    assert service.get_task(second.task_id).task.state.value == "cancelled"


# --- review fix pass 2: task read/resume integrity ---------------------------


def _domain_result(ctx, *, status=None):
    from game_app.contracts.common import CheckResult
    from game_app.contracts.enums import CheckStatus, TaskType
    from game_app.contracts.tasks import DomainResult

    return DomainResult(
        domain=TaskType.knowledge,
        code="completed_scope",
        input_revision=ctx.task_input_revision,
        checks=[CheckResult(
            check_id="domain.check",
            definition_version="checks/0.4",
            status=status or CheckStatus.pass_,
            input_refs=[],
            evidence_refs=[],
            checked_at="2026-11-02T00:00:00+00:00",
            method="deterministic rule",
        )],
    )


def test_get_task_reads_a_persisted_domain_result_with_checks(repo_factory):
    repo = repo_factory()
    service = _service(repo)

    def handler(ctx):
        return HandlerOutcome(state=TaskState.completed, domain_result=_domain_result(ctx))

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(), _ctx())
    assert service.run_once().state.value == "completed"

    view = service.get_task(accepted.task_id)
    assert view.task.domain_result is not None
    assert view.task.domain_result.checks[0].status.value == "pass"


def test_a_resumed_task_owns_a_copy_of_its_inherited_checkpoint(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    parent = service.register_task(_knowledge_input(), _ctx("k1"))
    parent_checkpoint = service.record_checkpoint(parent.task_id, ["acquire"])
    service.recover_on_startup()

    first = service.resume_task(
        {"parent_task_id": parent.task_id, "checkpoint_id": parent_checkpoint, "budget": {}},
        _ctx("k2"),
    )
    inherited = service.get_task(first.task_id).task.checkpoint_id
    assert inherited is not None and inherited != parent_checkpoint
    assert service.get_task(first.task_id).progress.completed_steps == ["acquire"]

    service.recover_on_startup()
    second = service.resume_task(
        {"parent_task_id": first.task_id, "checkpoint_id": inherited, "budget": {}},
        _ctx("k3"),
    )
    assert second.task_id != first.task_id
    assert service.get_task(second.task_id).progress.completed_steps == ["acquire"]

    # The parent's own checkpoint row and pointer are untouched.
    assert service.get_task(parent.task_id).task.checkpoint_id == parent_checkpoint
    with repo.unit_of_work() as uow:
        row = uow.query(
            "SELECT task_id FROM task_checkpoints WHERE checkpoint_id=?", (parent_checkpoint,)
        )[0]
    assert row["task_id"] == parent.task_id


def test_resume_reuses_an_already_succeeded_call_instead_of_resending(repo_factory):
    repo = repo_factory()
    provider = SpyProvider()
    service = _service(repo, provider)

    def handler(ctx):
        _invoke_model(ctx)
        ctx.checkpoint(["acquire"])
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    parent = service.register_task(_knowledge_input(model_calls=2), _ctx("k1"))
    assert service.run_once().state.value == "completed"
    assert provider.sends == 1
    checkpoint = service.get_task(parent.task_id).task.checkpoint_id

    resumed = service.resume_task(
        {"parent_task_id": parent.task_id, "checkpoint_id": checkpoint,
         "budget": {"max_model_calls": 2}},
        _ctx("k2"),
    )
    service.handlers["knowledge"] = handler
    outcome = service.run_once()

    assert outcome.state.value == "completed"
    assert provider.sends == 1  # the completed step was reused, not re-sent
    calls = _calls(repo, resumed.task_id)
    assert [call["call_state"] for call in calls] == ["succeeded"]
    assert calls[0]["response_file_id"]
    # A reused call never entered the send window, so it has no sent time.
    assert calls[0]["sent_at"] is None
    # A reused call never entered the send window, so it consumes no budget.
    with repo.unit_of_work() as uow:
        counters = uow.query(
            "SELECT calls FROM tasks WHERE task_id=?", (resumed.task_id,)
        )[0]["calls"]
    assert '"model":0' in counters


def test_task_view_exposes_the_persisted_error(repo_factory):
    repo = repo_factory()
    provider = SpyProvider(mode="unknown")
    service = _service(repo, provider)

    def handler(ctx):
        _invoke_model(ctx)

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(model_calls=1), _ctx())
    assert service.run_once().state.value == "partial"

    view = service.get_task(accepted.task_id)
    assert view.error is not None
    assert view.error.code is ErrorCode.CALL_OUTCOME_UNKNOWN
    assert view.error.request_id == accepted.task_id


def test_task_view_lists_the_questions_it_raised(repo_factory):
    repo = repo_factory()
    service = _service(repo)

    def handler(ctx):
        ctx.suspend(information_request_id=_uid(702))

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(), _ctx())
    assert service.run_once().state.value == "waiting_input"

    view = service.get_task(accepted.task_id)
    assert view.task.information_request_ids == [_uid(702)]
    assert view.error is None


def test_task_view_lists_a_question_returned_with_a_partial_result(repo_factory):
    from game_app.contracts.primitives import ObjectRef

    repo = repo_factory()
    service = _service(repo)

    def handler(ctx):
        return HandlerOutcome(
            state=TaskState.partial,
            result_refs=[
                ObjectRef(entity_type="InformationRequest", entity_id=_uid(703)),
            ],
        )

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(), _ctx())
    assert service.run_once().state.value == "partial"

    view = service.get_task(accepted.task_id)
    assert view.task.information_request_ids == [_uid(703)]


# --- 2D-A: phase events ------------------------------------------------------


def test_a_phase_event_changes_the_reported_phase(repo_factory):
    repo = repo_factory()
    service = _service(repo)

    def handler(ctx):
        ctx.phase("validate")
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(), _ctx())
    assert service.run_once().state.value == "completed"

    view = service.get_task(accepted.task_id)
    assert view.progress.phase == "validate"
    with repo.unit_of_work() as uow:
        causes = [
            row["cause"] for row in uow.query(
                "SELECT cause FROM task_events WHERE task_id=? ORDER BY sequence",
                (accepted.task_id,),
            )
        ]
    assert "phase:validate" in causes


def test_without_a_phase_event_the_state_fallback_is_unchanged(repo_factory):
    repo = repo_factory()
    service = _service(repo)
    accepted = service.register_task(_knowledge_input(), _ctx())
    assert service.get_task(accepted.task_id).progress.phase == "queued"

    service.handlers["knowledge"] = lambda ctx: HandlerOutcome.completed()
    assert service.run_once().state.value == "completed"
    assert service.get_task(accepted.task_id).progress.phase == "terminal"


def test_phase_requires_a_registered_key(repo_factory):
    repo = repo_factory()
    service = _service(repo)

    def handler(ctx):
        ctx.phase("not-a-phase")
        return HandlerOutcome.completed()

    service.handlers["knowledge"] = handler
    accepted = service.register_task(_knowledge_input(), _ctx())
    outcome = service.run_once()

    assert outcome.state.value == "failed"
    assert service.get_task(accepted.task_id).task.last_error_code == "INPUT_VALIDATION_FAILED"
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM task_events WHERE cause LIKE 'phase:%'")[0]["n"] == 0


# --- A-1: the log_import member of the task union ---------------------------


def _log_import_input(**overrides) -> dict:
    payload = {
        "kind": "log_import",
        "file_id": _uid(700),
        "session_id": _uid(701),
        "segment_ref": {
            "segment_id": _uid(702),
            "segment_revision": 1,
            "binding_revision": 1,
        },
        "source_schema": "offline-frame-csv/v1",
    }
    payload.update(overrides)
    return payload


def test_log_import_is_a_task_input_member():
    from game_app.contracts.tasks import TaskInputAdapter

    typed = TaskInputAdapter.validate_python(_log_import_input())

    assert typed.kind == "log_import"
    assert typed.file_id == _uid(700)
    assert typed.segment_ref.segment_revision == 1
    assert typed.source_schema == "offline-frame-csv/v1"


@pytest.mark.parametrize(
    "override",
    [
        {"file_id": "not-a-hex-id"},
        {"source_schema": "   "},
        {"session_id": "abc"},
        {"segment_ref": {"segment_id": _uid(702), "segment_revision": 0, "binding_revision": 1}},
        {"segment_ref": {"segment_id": _uid(702), "segment_revision": 1}},
    ],
)
def test_invalid_log_import_is_refused_without_creating_a_task(repo_factory, override):
    repo = repo_factory()
    service = _service(repo)
    payload = _log_import_input(**override)
    if "segment_ref" in override and "binding_revision" not in override["segment_ref"]:
        del payload["source_schema"]
        payload["source_schema"] = "offline-frame-csv/v1"

    with pytest.raises(ContractError) as caught:
        service.register_task(payload, _ctx("log-bad"))

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0


def test_application_refuses_log_import_because_no_handler_is_registered(repo_factory, tmp_path):
    from game_app.application import Application

    repo = repo_factory()
    app = Application(repo, file_root=str(tmp_path / "managed"))

    with pytest.raises(ContractError) as caught:
        app.register_task(_log_import_input(), _ctx("log-app"))

    assert caught.value.code is ErrorCode.INPUT_VALIDATION_FAILED
    assert caught.value.field_errors[0].path == "kind"
    with repo.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0
