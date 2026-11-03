"""KNW-T03: planned follow-up collection, stop rules and the call audit.

The provider is an in-process stub that answers the documented stub protocol
(search -> urls, read -> document, model -> records).  Every call still goes
through ``TaskContext.invoke_provider``: the assertions are about the durable
audit rows, not about the stub's counters alone.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from game_app.contracts.common import CheckResult, EvidenceRef, FieldValue, Provenance
from game_app.contracts.device import AdapterCapabilities, GameContext
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    CheckStatus,
    CollectionResult,
    GameIdentityStatus,
    TaskState,
    ValueState,
)
from game_app.contracts.knowledge import Game
from game_app.contracts.tasks import KnowledgeInput
from game_app.contracts.common import MutationContext
from game_app.contracts.primitives import utc_now
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileService
from game_app.knowledge import KnowledgeService
from game_app.repository import ProductRepository

SCOPE = "a" * 32
NOW = "2026-11-03T00:00:00+00:00"
URL = "https://example.invalid/guide"


def _uid(value: int) -> str:
    return f"{value:032x}"


def _not_run(check_id: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version="test",
        status=CheckStatus.not_run,
        input_refs=[],
        evidence_refs=[],
        checked_at=None,
        method="test stub",
    )


def _repository(tmp_path: Path) -> ProductRepository:
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    return repository


def _store_game(repository: ProductRepository) -> Game:
    game = Game(
        id=_uid(100),
        schema_version="product-contract/0.4",
        created_at=NOW,
        created_by_task_id=None,
        series_id=_uid(101),
        version=1,
        canonical_name="Synthetic Game",
        aliases=[],
        platform_scope=["steam"],
        identity_status=GameIdentityStatus.verified,
        identity_checks=[CheckResult(
            check_id="identity.evidence", definition_version="test",
            status=CheckStatus.pass_, input_refs=[], evidence_refs=[],
            checked_at=NOW, method="test",
        )],
        declaring_user_scope_id=None,
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("Game", game)
    return game


def _store_context(repository: ProductRepository, files: FileService, game: Game) -> GameContext:
    file_id = files.store_artifact(
        b'{"kind":"test-declaration"}',
        relative_name="test/declaration.json",
        stable_key="declaration:collection",
    )
    stored = repository.get_object("ManagedFile", file_id)
    context = GameContext(
        id=_uid(300),
        schema_version="product-contract/0.4",
        created_at=NOW,
        created_by_task_id=None,
        series_id=_uid(300),
        version=1,
        game_id=game.series_id,
        game_revision_id=game.id,
        installation_id=None,
        platform=FieldValue[str](
            value_state=ValueState.known,
            value="steam",
            acquisition_status=AcquisitionStatus.succeeded,
            provenance=[Provenance(
                method=AcquisitionMethod.user_provided,
                observed_at=None,
                recorded_at=NOW,
                evidence_refs=[EvidenceRef(
                    kind="artifact",
                    file_id=file_id,
                    content_hash=stored.payload["manifest"]["sha256"],
                    locator="test/declaration.json",
                )],
            )],
        ),
        build_identifiers=[],
        process_candidates=[],
        selected_process=None,
        adapter_id=None,
        adapter_version=None,
        capabilities=AdapterCapabilities(
            can_identify=_not_run("adapter.identify"),
            can_read_config=_not_run("adapter.read_config"),
            can_bind_capture=_not_run("adapter.bind_capture"),
        ),
        checks=[],
    )
    with repository.unit_of_work() as uow:
        uow.insert_versioned("GameContext", context)
    return context


# --- the stub provider -------------------------------------------------------

DEFAULT_RECORDS = [
    {
        "local_id": "set-1",
        "record_type": "settings",
        "payload": {
            "id": "set-1", "raw_name": "Quality", "canonical_name": "Quality",
            "aliases": [], "proof": {"url": URL},
        },
    },
    {
        "local_id": "evi-1",
        "record_type": "evidence",
        "payload": {
            "id": "evi-1", "kind": "measurement", "origin": "source_statement",
            "statement": "synthetic average frame rate", "metric": "average_fps",
            "numeric": {"number_kind": "exact", "value": 60}, "unit": "fps",
            "setting_ids": ["set-1"], "input_ids": [], "detected_state": None,
        },
    },
    {
        "local_id": "cfg-1",
        "record_type": "configurations",
        "payload": {
            "id": "cfg-1", "name": "Preset", "kind": "resolved_configuration",
            "assignments": [{
                "setting_id": "set-1",
                "value": {"value_kind": "fixed", "value": "High"},
                "evidence_ids": ["evi-1"],
            }],
            "preset_reference": None,
            "environment_ids": [],
        },
    },
]


class StubCollectionProvider:
    """In-process stub: no network, one URL per search, records per extraction."""

    def __init__(
        self,
        *,
        records=DEFAULT_RECORDS,
        empty: bool = False,
        mode: str = "ok",
        return_model: bool = True,
        bad_records: int = 0,
        url: str = URL,
    ) -> None:
        self.records = list(records)
        self.empty = empty
        self.mode = mode
        self.return_model = return_model
        self.bad_records = bad_records
        self.url = url
        self.sends = 0
        self.calls: list[dict] = []

    def send(self, request):
        self.sends += 1
        self.calls.append(dict(request))
        kind = request["kind"]
        if self.mode == "unknown" and kind == "read":
            raise TimeoutError("simulated unknown outcome after in_flight")
        if kind == "search":
            return {"urls": [self.url]}
        if kind == "read":
            return {"document": {"content": "synthetic article body"}}
        rows = [] if self.empty else list(self.records)
        for _ in range(self.bad_records):
            rows.append({"record_type": "not_a_record_type", "payload": {}})
            rows.append({"record_type": "settings", "payload": None})
        reply = {
            "records": rows,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
        if self.return_model:
            reply["returned_model"] = "stub-model-1"
        else:
            # The legacy compatibility field; it must never become a product
            # ``returned_model``.
            reply["model"] = "compat-model-name"
        return reply


# --- harness -----------------------------------------------------------------


def _knowledge_input(context: GameContext, *, key: str, prior: str | None = None, **budget):
    values = {
        "max_urls": 12, "max_searches": 12, "max_followup_rounds": 2,
        "max_queries_per_gap": 2, "max_model_calls": 16, "max_read_calls": 36,
    }
    values.update(budget)
    return (
        KnowledgeInput(
            kind="knowledge",
            game_context_id=context.id,
            device_snapshot_id=None,
            prior_package_id=prior,
            budget=values,
        ),
        MutationContext(user_scope_id=SCOPE, idempotency_key=key, request_id=_uid(4000)),
    )


def _app(repository: ProductRepository, files: FileService, provider):
    from game_app.tasks import TaskService

    service = TaskService(repository, provider=provider, files=files)
    knowledge = KnowledgeService(repository, files)
    service.handlers["knowledge"] = knowledge.handle
    return service, knowledge


def _run(service, task_input, mutation):
    accepted = service.register_task(task_input, mutation)
    outcome = service.run_once()
    return accepted, outcome, service.get_task(accepted.task_id)


def _calls(repository: ProductRepository, kind: str | None = None):
    with repository.unit_of_work() as uow:
        if kind is None:
            return uow.query("SELECT * FROM external_calls ORDER BY rowid")
        return uow.query(
            "SELECT * FROM external_calls WHERE kind=? ORDER BY rowid", (kind,)
        )


def _count(repository: ProductRepository, entity_type: str) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


# --- tests -------------------------------------------------------------------


def test_collection_starts_without_a_target_fps_preference_or_device(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    provider = StubCollectionProvider()
    service, _knowledge = _app(repository, files, provider)
    task_input, mutation = _knowledge_input(context, key="task-1")

    accepted = service.register_task(task_input, mutation)
    outcome = service.run_once()

    assert accepted.state is TaskState.queued
    assert outcome.state in (TaskState.completed, TaskState.partial)
    assert provider.sends == 3  # one search, one read, one extraction
    assert _count(repository, "KnowledgePackage") == 1


def test_two_rounds_without_gain_stop_with_low_information_gain(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    provider = StubCollectionProvider(empty=True)
    service, _knowledge = _app(repository, files, provider)
    task_input, mutation = _knowledge_input(context, key="task-1")

    _accepted, outcome, view = _run(service, task_input, mutation)

    assert outcome.state is TaskState.partial
    assert view.task.stop_reason.kind == "collection"
    assert view.task.stop_reason.code == "low_information_gain"
    # Two rounds; the second round searches again but never re-reads the same
    # canonical URL, so the read and extraction each happened once.
    assert len(_calls(repository, "search")) == 2
    assert len(_calls(repository, "read")) == 1
    assert len(_calls(repository, "model")) == 1
    assert provider.sends == 4
    gain_check = next(
        check for check in view.task.domain_result.checks
        if check.check_id == "collection.rounds"
    )
    assert "2 follow-up round(s)" in gain_check.note


def test_budget_exhaustion_keeps_the_members_and_gaps_already_found(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)

    # First run: the default stub yields records, so a package is published.
    first = _app(repository, files, StubCollectionProvider())
    task_input, mutation = _knowledge_input(context, key="task-1")
    _accepted, _outcome, _view = _run(first[0], task_input, mutation)
    with repository.unit_of_work() as uow:
        packages = uow.latest_objects("KnowledgePackage")
    assert len(packages) == 1
    prior_package_id = packages[0].object_id

    # Second run against the open package with a budget that allows one call
    # of each kind: the first round gains records, the next search cannot
    # start, and the package keeps what was found.
    provider = StubCollectionProvider()
    service, _knowledge = _app(repository, files, provider)
    task_input, mutation = _knowledge_input(
        context, key="task-2", prior=prior_package_id,
        max_searches=1, max_read_calls=1, max_model_calls=1,
    )
    _accepted, outcome, view = _run(service, task_input, mutation)

    assert outcome.state is TaskState.partial
    assert view.task.stop_reason.kind == "collection"
    assert view.task.stop_reason.code == "budget_exhausted"
    with repository.unit_of_work() as uow:
        latest = uow.latest_objects("KnowledgePackage")[0]
    package = json.loads(latest.payload_bytes)
    assert latest.object_id != prior_package_id
    assert package["record_snapshot_ids"]
    assert package["critical_gaps"]
    # This package aggregates two runs, so the three execution facts stay null
    # and only the task reports the stop reason (02 §4).
    assert package["collection_result"] is None
    assert package["stop_reason"] is None
    assert package["collection_execution_ref"] is None


def test_one_url_with_several_bad_records_is_read_once(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    provider = StubCollectionProvider(bad_records=2)
    service, _knowledge = _app(repository, files, provider)
    task_input, mutation = _knowledge_input(
        context, key="task-1", max_followup_rounds=1
    )
    _accepted, _outcome, view = _run(service, task_input, mutation)

    assert len(_calls(repository, "read")) == 1
    error_check = next(
        check for check in view.task.domain_result.checks
        if check.check_id == "collection.read_errors"
    )
    assert error_check.status is CheckStatus.fail
    assert "record[3]" in error_check.note
    assert "record[4]" in error_check.note


def test_an_unknown_call_stops_without_a_second_send_on_retry_or_resume(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    provider = StubCollectionProvider(mode="unknown")
    service, _knowledge = _app(repository, files, provider)
    task_input, mutation = _knowledge_input(context, key="task-1")

    accepted, outcome, view = _run(service, task_input, mutation)

    assert outcome.task_id == accepted.task_id
    assert outcome.state is TaskState.partial
    assert view.task.last_error_code == ErrorCode.CALL_OUTCOME_UNKNOWN.value
    assert provider.sends == 2  # search + the read that may have been delivered
    unknown_rows = [
        row for row in _calls(repository)
        if row["call_state"] == "outcome_unknown"
    ]
    assert len(unknown_rows) == 1

    # Same key: the original TaskAccepted is replayed, nothing runs.
    replay = service.register_task(task_input, mutation)
    assert replay.task_id == accepted.task_id
    assert service.run_once() is None
    assert provider.sends == 2

    # Explicit resume: the chain has an unknown call, so the new task is
    # finished partial without entering the handler.
    resumed = service.resume_task(
        {
            "parent_task_id": accepted.task_id,
            "checkpoint_id": None,
            "budget": {"max_urls": 12, "max_searches": 12,
                       "max_followup_rounds": 2, "max_queries_per_gap": 2,
                       "max_model_calls": 16, "max_read_calls": 36},
        },
        MutationContext(user_scope_id=SCOPE, idempotency_key="resume-1",
                        request_id=_uid(4001)),
    )
    resumed_outcome = service.run_once()

    assert resumed_outcome.task_id == resumed.task_id
    assert resumed_outcome.state is TaskState.partial
    assert provider.sends == 2
    resumed_view = service.get_task(resumed.task_id)
    assert resumed_view.task.last_error_code == ErrorCode.CALL_OUTCOME_UNKNOWN.value


def test_a_missing_returned_model_stays_null(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    provider = StubCollectionProvider(return_model=False)
    service, _knowledge = _app(repository, files, provider)
    task_input, mutation = _knowledge_input(context, key="task-1")

    _accepted, _outcome, _view = _run(service, task_input, mutation)

    model_calls = _calls(repository, "model")
    assert len(model_calls) == 1
    assert model_calls[0]["returned_model"] is None
    # The requested name is recorded separately; the compatibility field from
    # the reply never becomes the returned model.
    assert model_calls[0]["requested_model"] == "stub-model"
    with repository.unit_of_work() as uow:
        extractions = uow.iter_objects("ImportedExtraction")
    assert extractions
    assert all(
        json.loads(item.payload_bytes)["returned_model"] is None for item in extractions
    )


def test_a_new_collection_becomes_a_standard_group_and_one_task(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    provider = StubCollectionProvider()
    service, _knowledge = _app(repository, files, provider)
    task_input, mutation = _knowledge_input(context, key="task-1")

    accepted, _outcome, _view = _run(service, task_input, mutation)

    with repository.unit_of_work() as uow:
        task_rows = uow.query("SELECT type FROM tasks")
    assert len(task_rows) == 1
    assert task_rows[0]["type"] == "knowledge"
    with repository.unit_of_work() as uow:
        assert uow.query(
            "SELECT COUNT(*) AS n FROM tasks WHERE type='knowledge_import'"
        )[0]["n"] == 0
    with repository.unit_of_work() as uow:
        snapshots = uow.iter_objects("ImportSnapshot")
    assert len(snapshots) == 1
    snapshot = json.loads(snapshots[0].payload_bytes)
    assert snapshot["origin"] == "collector_run"
    assert snapshot["import_status"] == "published"
    names = {entry["relative_name"] for entry in snapshot["manifest"]["entries"]}
    assert names == {"run.json", "sources.json", "records.json",
                     "gaps.json", "extractions.json"}
    with repository.unit_of_work() as uow:
        package_row = uow.latest_objects("KnowledgePackage")[0]
    package = json.loads(package_row.payload_bytes)
    assert package["import_snapshot_ids"] == [snapshot["id"]]
    assert package["record_snapshot_ids"]
    assert package["collection_execution_ref"] == {
        "entity_type": "TaskRun", "entity_id": accepted.task_id,
    }
    with repository.unit_of_work() as uow:
        records = uow.iter_objects("ImportedRecord")
    assert records
    assert all(
        json.loads(record.payload_bytes)["import_snapshot_id"] == snapshot["id"]
        for record in records
    )


def test_the_group_bytes_are_reproducible_for_the_same_task(tmp_path):
    from game_app.collection import CollectionSnapshotWriter

    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    writer = CollectionSnapshotWriter(repository, files)
    kwargs = dict(
        task_id=_uid(500),
        run_id="run-stable",
        game_series_id=game.series_id,
        user_scope_id=SCOPE,
        started_at=NOW,
        collection_result=CollectionResult.partial_scope,
        stop_reason=None,
        sources=[],
        gaps=[],
    )

    first, first_mapping = writer.write(**kwargs)
    second, second_mapping = writer.write(**kwargs)

    assert first.file_ids == second.file_ids
    assert first.manifest == second.manifest
    assert first_mapping == second_mapping


def test_every_sent_call_is_audited_and_nothing_was_sent_without_a_record(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    provider = StubCollectionProvider()
    service, _knowledge = _app(repository, files, provider)
    task_input, mutation = _knowledge_input(context, key="task-1")

    _run(service, task_input, mutation)

    rows = _calls(repository)
    assert len(rows) == provider.sends == 3
    # Every audited call entered the send window; none is left "prepared".
    assert all(row["call_state"] == "succeeded" for row in rows)
    assert all(row["sent_at"] for row in rows)
    assert {row["kind"] for row in rows} == {"search", "read", "model"}


def test_an_unresolved_identity_is_a_pure_conflict_before_registration(tmp_path):
    repository = _repository(tmp_path)
    files = FileService(repository, tmp_path / "managed")
    game = _store_game(repository)
    context = _store_context(repository, files, game)
    knowledge = KnowledgeService(repository, files)
    task_input, _mutation = _knowledge_input(context, key="task-1")
    # Break the context's game reference: identity is no longer resolvable.
    with repository.unit_of_work() as uow:
        stored = uow.find_object("GameContext", context.id)
        payload = json.loads(stored.payload_bytes)
    payload["game_revision_id"] = _uid(9999)
    with repository.unit_of_work() as uow:
        uow.execute(
            "UPDATE objects SET payload=? WHERE id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), context.id),
        )

    with pytest.raises(ContractError) as caught:
        knowledge.precheck_collection(task_input, user_scope_id=SCOPE)

    assert caught.value.code is ErrorCode.REFERENCE_INVALID
    with repository.unit_of_work() as uow:
        assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0
