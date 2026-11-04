"""ENG-T04 (CLI): one command per Application entry, shared envelope and codes.

Every fixture is a temporary database and a local user scope.  No provider,
network, image or real game is involved; a task that needs a provider reports
that state in its own TaskView while the command itself still succeeds.
"""

from __future__ import annotations

import json

import pytest

from game_app import cli
from game_app.application import Application
from game_app.contracts.performance import CreateSessionRequest
from game_app.repository import ProductRepository, canonical_json
from tests.product.support.configuration import USER, context_key, setup
from tests.product.test_sessions import FakeClock

OTHER = "c" * 32


# --- fixtures ---------------------------------------------------------------


class Fixture:
    def __init__(self, tmp_path, session_id, segment_id):
        self.tmp_path = tmp_path
        self.session_id = session_id
        self.segment_id = segment_id
        self.db = str(tmp_path / "product.sqlite3")
        self.files = str(tmp_path / "managed")


def _fixture(tmp_path) -> Fixture:
    repository, files, installation, context_id, device_id = setup(tmp_path)
    application = Application(
        repository, file_root=str(tmp_path / "managed"), clock=FakeClock(),
    )
    request = CreateSessionRequest.model_validate_json(canonical_json({
        "installation_id": installation,
        "device_snapshot_id": device_id,
        "game_context_id": context_id,
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
    result = application.create_session(request, context_key("cli-session"))
    session_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "PlaySession"
    )
    segment_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "SessionSegment"
    )
    repository.close()
    return Fixture(tmp_path, session_id, segment_id)


def _input_file(tmp_path, payload, name="input.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _args(fixture: Fixture, *command, key=None, scope=USER, wait=False) -> list[str]:
    args = list(command)
    args += ["--db", fixture.db, "--files", fixture.files]
    if scope is not None:
        args += ["--user-scope", scope]
    if key is not None:
        args += ["--key", key]
    if wait:
        args.append("--wait")
    return args


def _run(capsys, argv):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _feedback_payload(fixture, text):
    return {
        "kind": "feedback",
        "session_id": fixture.session_id,
        "segment_id": None,
        "raw_text": text,
        "selections": {"feeling": "want_improvement"},
    }


def _object_count(fixture, entity_type):
    repository = ProductRepository(fixture.db)
    try:
        with repository.unit_of_work() as uow:
            return len(uow.iter_objects(entity_type))
    finally:
        repository.close()


def _task_count(fixture):
    repository = ProductRepository(fixture.db)
    try:
        with repository.unit_of_work() as uow:
            return uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"]
    finally:
        repository.close()


# --- tests ------------------------------------------------------------------


def test_help_lists_every_command_and_writes_nothing(tmp_path, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--help"])
    assert stopped.value.code == 0
    out = capsys.readouterr().out
    for command in (
        "feedback", "next-action", "preference", "target-acceptance", "outcome",
        "recommend", "task", "consume-impact", "session", "segment", "summary",
        "comparison", "package", "recommendation", "diagnosis",
    ):
        assert command in out
    assert not (tmp_path / "product.sqlite3").exists()


def test_a_forged_field_is_refused_before_any_write(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    payload = _feedback_payload(fixture, "the scene stutters")
    payload["provenance"] = {"method": "system_probe"}
    path = _input_file(tmp_path, payload)

    code, out, err = _run(
        capsys, _args(fixture, "feedback", "record", "--input", path, key="k1"),
    )

    assert code == 2
    assert out == ""
    assert json.loads(err)["code"] == "INPUT_VALIDATION_FAILED"
    assert _object_count(fixture, "Feedback") == 0


def test_a_forged_controlled_flag_is_refused(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    payload = _feedback_payload(fixture, "looks smoother")
    payload["controlled"] = True
    path = _input_file(tmp_path, payload)

    code, _, err = _run(
        capsys, _args(fixture, "feedback", "record", "--input", path, key="k1"),
    )

    assert code == 2
    assert json.loads(err)["code"] == "INPUT_VALIDATION_FAILED"


def test_a_missing_key_is_refused(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    path = _input_file(tmp_path, _feedback_payload(fixture, "the scene stutters"))

    code, _, err = _run(
        capsys, _args(fixture, "feedback", "record", "--input", path, key=None),
    )

    assert code == 2
    assert json.loads(err)["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_a_missing_user_scope_is_a_security_error(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    path = _input_file(tmp_path, _feedback_payload(fixture, "the scene stutters"))

    code, _, err = _run(
        capsys,
        _args(fixture, "feedback", "record", "--input", path, key="k1", scope=None),
    )

    assert code == 3
    assert json.loads(err)["code"] == "SESSION_CONTEXT_INVALID"


def test_the_same_key_with_different_content_conflicts(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    first = _input_file(tmp_path, _feedback_payload(fixture, "one"), "one.json")
    second = _input_file(tmp_path, _feedback_payload(fixture, "two"), "two.json")

    assert _run(capsys, _args(
        fixture, "feedback", "record", "--input", first, key="same",
    ))[0] == 0
    code, _, err = _run(capsys, _args(
        fixture, "feedback", "record", "--input", second, key="same",
    ))

    assert code == 3
    assert json.loads(err)["code"] == "IDEMPOTENCY_CONFLICT"
    assert _task_count(fixture) == 1


def test_another_players_task_is_not_found(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    path = _input_file(tmp_path, _feedback_payload(fixture, "the scene stutters"))
    _, out, _ = _run(
        capsys, _args(fixture, "feedback", "record", "--input", path, key="mine"),
    )
    task_id = json.loads(out)["task_id"]

    read_code, _, read_err = _run(
        capsys, _args(fixture, "task", "get", task_id, scope=OTHER),
    )
    cancel_code, _, cancel_err = _run(
        capsys,
        _args(fixture, "task", "cancel", task_id, key="foreign", scope=OTHER),
    )

    assert read_code == cancel_code == 4
    assert json.loads(read_err)["code"] == "RESOURCE_NOT_FOUND"
    assert json.loads(cancel_err)["code"] == "RESOURCE_NOT_FOUND"
    stored = ProductRepository(fixture.db)
    try:
        with stored.unit_of_work() as uow:
            row = uow.query("SELECT state FROM tasks WHERE task_id=?", (task_id,))[0]
    finally:
        stored.close()
    assert row["state"] == "queued"


def test_the_same_key_and_content_replay_the_same_bytes(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    path = _input_file(tmp_path, _feedback_payload(fixture, "the scene stutters"))
    argv = _args(fixture, "feedback", "record", "--input", path, key="replay")

    first_code, first_out, _ = _run(capsys, argv)
    second_code, second_out, _ = _run(capsys, argv)

    assert first_code == second_code == 0
    assert first_out == second_out
    assert json.loads(first_out)["state"] == "queued"
    assert _task_count(fixture) == 1


def test_another_players_session_is_not_found(tmp_path, capsys):
    fixture = _fixture(tmp_path)

    code, _, err = _run(
        capsys, _args(fixture, "session", "get", fixture.session_id, scope=OTHER),
    )

    assert code == 4
    assert json.loads(err)["code"] == "RESOURCE_NOT_FOUND"
    assert json.loads(err)["resource_refs"] == []


def test_a_read_command_changes_no_object_count(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    before = _object_count(fixture, "PlaySession")
    segments = _object_count(fixture, "SessionSegment")

    code, out, _ = _run(capsys, _args(fixture, "session", "get", fixture.session_id))

    assert code == 0
    assert json.loads(out)["id"] == fixture.session_id
    assert _object_count(fixture, "PlaySession") == before
    assert _object_count(fixture, "SessionSegment") == segments


def test_without_wait_the_command_only_registers_the_task(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    path = _input_file(tmp_path, _feedback_payload(fixture, "the scene stutters"))

    code, out, _ = _run(
        capsys, _args(fixture, "feedback", "record", "--input", path, key="queued"),
    )

    payload = json.loads(out)
    assert code == 0
    assert payload["state"] == "queued"
    assert set(payload) == {"task_id", "input_revision", "state"}


def test_wait_prints_the_terminal_state_and_a_task_error_is_not_a_command_error(
    tmp_path, capsys,
):
    fixture = _fixture(tmp_path)
    path = _input_file(tmp_path, _feedback_payload(fixture, "the scene stutters"))

    code, out, _ = _run(
        capsys,
        _args(fixture, "feedback", "record", "--input", path, key="waited", wait=True),
    )

    payload = json.loads(out)
    assert code == 0
    assert payload["task"]["state"] == "partial"
    assert payload["error"]["code"] == "PROVIDER_NOT_CONFIGURED"
    assert payload["task"]["domain_result"]["code"] == "unparsed"


def test_task_run_reports_the_claimed_task(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    path = _input_file(tmp_path, _feedback_payload(fixture, "the scene stutters"))
    _, accepted_out, _ = _run(
        capsys, _args(fixture, "feedback", "record", "--input", path, key="run-1"),
    )
    accepted = json.loads(accepted_out)

    code, out, _ = _run(capsys, ["task", "run", "--db", fixture.db])

    payload = json.loads(out)
    assert code == 0
    assert payload["task_id"] == accepted["task_id"]
    assert payload["state"] == "partial"


def test_a_failed_task_keeps_the_command_successful_for_next_action(tmp_path, capsys):
    fixture = _fixture(tmp_path)
    payload = {
        "kind": "next_action",
        "session_id": fixture.session_id,
        "bindings": [],
        "summary_ids": [],
        "feedback_ids": [],
        "current_applied_id": None,
        "package_id": None,
    }
    path = _input_file(tmp_path, payload)

    code, out, _ = _run(
        capsys,
        _args(fixture, "next-action", "plan", "--input", path, key="na", wait=True),
    )

    body = json.loads(out)
    assert code == 0
    assert body["task"]["state"] == "completed"
    assert body["task"]["domain_result"]["code"] in {
        "insufficient_evidence", "measure", "research", "keep",
    }
