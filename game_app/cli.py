"""ENG-T04: the command-line entry point over ``Application``.

The CLI adds no business rule.  It parses JSON, resolves the local user scope
and the idempotency key, calls one ``Application`` entry, and prints
``canonical_json`` or the shared error envelope.  An asynchronous command only
registers the task (202 semantics) unless ``--wait`` asks the caller to advance
the single execution slot; nothing polls silently.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import fields, is_dataclass
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from game_app.adapters.windows_probe import WindowsProbeAdapter
from game_app.application import Application
from game_app.contracts.common import MutationContext
from game_app.contracts.configuration import (
    AdoptConfigurationInput,
    HistoricalConfigurationInput,
)
from game_app.contracts.device import (
    CreateInstallationRequest,
    InstallationCatalogQuery,
    RegisterLocalSettingRequest,
)
from game_app.contracts.information import (
    CreateInformationAmendmentRequest,
    InformationResponseInput,
)
from game_app.contracts.knowledge import (
    GameCatalogQuery,
    GameSelectionRequest,
    ImportIdentityMappingRequest,
)
from game_app.contracts.performance import (
    BindConfigurationRequest,
    CompareInput,
    CreateComparisonProtocolRequest,
    CreateSessionRequest,
    CreateTestProtocolRequest,
    EndSegmentRequest,
    EndSessionRequest,
    LogAssociationRequest,
    MetricCatalogQuery,
    SplitSegmentRequest,
)
from game_app.contracts.tasks import (
    AnalysisInput,
    DeviceScanInput,
    KnowledgeImportInput,
    KnowledgeInput,
)
from game_app.errors import ContractError, ErrorCode
from game_app.lock import ApplicationLock
from game_app.repository import ProductRepository, canonical_json
from pydantic import ValidationError

CONTRACT_VERSION = "product-contract/0.4"
DEFAULT_DB = "product.sqlite3"
DEFAULT_FILES = "managed"
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# A bounded wait: each run claims at most one task, so an unrelated queued task
# ahead of ours is executed first and the view is still the requested task's.
_WAIT_LIMIT = 64

# E-R24: exit codes by error category.  A failed *task* is a successful
# command; it is reported in the printed TaskView, not in the exit code.
_EXIT_BY_CATEGORY = {
    "validation": 2,
    "dependency": 2,
    "conflict": 3,
    "security": 3,
    "not_found": 4,
    "external": 5,
    "storage": 6,
    "internal": 6,
}


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=None, help="product database path")
    parser.add_argument("--files", default=None, help="managed-file root")
    parser.add_argument(
        "--user-scope", default=None,
        help="local user scope id (never a remote caller's claim)",
    )
    parser.add_argument("--request-id", default=None, help="request correlation id")
    parser.add_argument(
        "--contract-version", default=CONTRACT_VERSION,
        help="request schema version used for the idempotency hash",
    )


def _write_common(parser: argparse.ArgumentParser) -> None:
    _common(parser)
    parser.add_argument("--input", required=True, help="JSON file or - for stdin")
    parser.add_argument("--key", default=None, help="idempotency key")
    parser.add_argument(
        "--wait", action="store_true",
        help="advance the execution slot and print the final task state",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="game-app",
        description="Player-side graphics optimization entry point",
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    feedback = commands.add_parser("feedback", help="save words and parse them")
    feedback_commands = feedback.add_subparsers(dest="action", required=True)
    _write_common(feedback_commands.add_parser("record", help="register feedback"))

    next_action = commands.add_parser("next-action", help="plan the next action")
    next_commands = next_action.add_subparsers(dest="action", required=True)
    _write_common(next_commands.add_parser("plan", help="register a diagnosis"))

    preference = commands.add_parser("preference", help="explicit preferences")
    preference_commands = preference.add_subparsers(dest="action", required=True)
    _write_common(preference_commands.add_parser("declare", help="declare a version"))

    acceptance = commands.add_parser("target-acceptance", help="accept or reject a target")
    acceptance_commands = acceptance.add_subparsers(dest="action", required=True)
    _write_common(acceptance_commands.add_parser("record", help="record acceptance"))

    outcome = commands.add_parser("outcome", help="experiment outcomes")
    outcome_commands = outcome.add_subparsers(dest="action", required=True)
    _write_common(outcome_commands.add_parser("record", help="record an outcome"))
    outcome_get = outcome_commands.add_parser("get", help="read an outcome")
    _common(outcome_get)
    outcome_get.add_argument("id")

    recommend = commands.add_parser("recommend", help="register a recommendation task")
    _write_common(recommend)

    consume = commands.add_parser("consume-impact", help="consume an impact notice")
    _common(consume)
    consume.add_argument("notice_id")
    consume.add_argument("--key", default=None, help="idempotency key")

    task = commands.add_parser("task", help="task control and inspection")
    task_commands = task.add_subparsers(dest="action", required=True)
    task_get = task_commands.add_parser("get", help="read a task")
    _common(task_get)
    task_get.add_argument("task_id")
    task_run = task_commands.add_parser("run", help="execute at most one queued task")
    _common(task_run)
    task_cancel = task_commands.add_parser("cancel", help="cancel a queued task")
    _common(task_cancel)
    task_cancel.add_argument("task_id")
    task_cancel.add_argument("--key", default=None, help="idempotency key")
    task_resume = task_commands.add_parser("resume", help="resume a finished task")
    _common(task_resume)
    task_resume.add_argument("--input", required=True, help="JSON file or -")
    task_resume.add_argument("--key", default=None, help="idempotency key")

    # -- 02 identity: the catalog and the first selection -------------------
    game = commands.add_parser("game", help="the local game catalog")
    game_commands = game.add_subparsers(dest="action", required=True)
    game_list = game_commands.add_parser("list", help="read the catalog")
    _common(game_list)
    game_list.add_argument("--query", default="")
    game_list.add_argument("--limit", type=int, default=20)
    game_list.add_argument("--cursor", default=None)
    _write_common(game_commands.add_parser("select", help="select a game by name"))

    # -- 01 context: installations and local setting entries ----------------
    installation = commands.add_parser("installation", help="this machine's installations")
    installation_commands = installation.add_subparsers(dest="action", required=True)
    installation_list = installation_commands.add_parser("list", help="read installations")
    _common(installation_list)
    installation_list.add_argument("--game-id", required=True)
    installation_list.add_argument("--limit", type=int, default=20)
    installation_list.add_argument("--cursor", default=None)
    _write_common(installation_commands.add_parser(
        "register", help="register an installation",
    ))

    local_setting = commands.add_parser("local-setting", help="local setting entries")
    local_setting_commands = local_setting.add_subparsers(dest="action", required=True)
    local_setting_list = local_setting_commands.add_parser("list", help="read entries")
    _common(local_setting_list)
    local_setting_list.add_argument("--installation-id", required=True)
    _write_common(local_setting_commands.add_parser(
        "register", help="register an entry",
    ))

    # -- 01 device acquisition ---------------------------------------------
    device = commands.add_parser("device", help="read-only device acquisition")
    device_commands = device.add_subparsers(dest="action", required=True)
    device_scan = device_commands.add_parser("scan", help="register a device scan")
    _write_common(device_scan)
    device_scan.add_argument(
        "--probe", choices=("none", "windows"), default="none",
        help="the only adapter this build can run is the packaged read-only"
             " Windows probe; 'none' leaves the device type unregistered",
    )

    # -- 07 managed files ---------------------------------------------------
    file_command = commands.add_parser("file", help="managed files")
    file_commands = file_command.add_subparsers(dest="action", required=True)
    file_upload = file_commands.add_parser("upload", help="store one local file")
    _common(file_upload)
    file_upload.add_argument("path")
    file_upload.add_argument("--name", default=None, help="stored name")
    file_upload.add_argument("--key", default=None, help="idempotency key")

    # -- 02 knowledge, 02a import ------------------------------------------
    knowledge = commands.add_parser("knowledge", help="knowledge collection")
    knowledge_commands = knowledge.add_subparsers(dest="action", required=True)
    _write_common(knowledge_commands.add_parser(
        "collect", help="register a knowledge task",
    ))

    importer = commands.add_parser("import", help="frozen collector groups")
    import_commands = importer.add_subparsers(dest="action", required=True)
    _write_common(import_commands.add_parser("run", help="register a knowledge import"))
    _write_common(import_commands.add_parser(
        "declare-identity",
        help="name the game one collector run belongs to",
    ))

    # -- 04 sessions, adoption, analysis ------------------------------------
    session = commands.add_parser("session", help="play sessions and segments")
    session_commands = session.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("create", "open a session and its first segment"),
        ("end-segment", "close the active segment"),
        ("split", "split the active segment"),
        ("end", "close every segment of a session"),
        ("bind", "append a configuration binding"),
    ):
        _write_common(session_commands.add_parser(action, help=help_text))
    session_get = session_commands.add_parser("get", help="read a play session")
    _common(session_get)
    session_get.add_argument("id")

    _write_common(commands.add_parser("adopt", help="record an applied configuration"))

    historical = commands.add_parser(
        "historical-configuration", help="backfill a past configuration",
    )
    historical_commands = historical.add_subparsers(dest="action", required=True)
    _write_common(historical_commands.add_parser("record", help="register a history entry"))

    _write_common(commands.add_parser("analyze", help="analyze a closed segment"))
    _write_common(commands.add_parser("compare", help="compare two summaries"))

    # -- 01 answers and facts-only corrections -----------------------------
    _write_common(commands.add_parser("answer", help="answer a waiting question"))
    amendment = commands.add_parser("amendment", help="facts-only corrections")
    amendment_commands = amendment.add_subparsers(dest="action", required=True)
    _write_common(amendment_commands.add_parser("create", help="create a facts-only request"))

    # -- 04 logs and protocols ---------------------------------------------
    artifact = commands.add_parser("artifact", help="capture artifacts")
    artifact_commands = artifact.add_subparsers(dest="action", required=True)
    artifact_get = artifact_commands.add_parser("get", help="read a capture artifact")
    _common(artifact_get)
    artifact_get.add_argument("id")

    log_command = commands.add_parser("log", help="capture logs")
    log_commands = log_command.add_subparsers(dest="action", required=True)
    log_inspect = log_commands.add_parser("inspect", help="preview a log read-only")
    _common(log_inspect)
    log_inspect.add_argument("file_id")
    log_inspect.add_argument("--schema", required=True)
    _write_common(log_commands.add_parser("associate", help="associate a log with a segment"))

    metric = commands.add_parser("metric", help="packaged metric rules")
    metric_commands = metric.add_subparsers(dest="action", required=True)
    metric_list = metric_commands.add_parser("list", help="read the rule catalog")
    _common(metric_list)
    metric_list.add_argument("--frame-stream-kind", default=None)

    test_protocol = commands.add_parser("test-protocol", help="test protocols")
    test_protocol_commands = test_protocol.add_subparsers(dest="action", required=True)
    _write_common(test_protocol_commands.add_parser(
        "create", help="create a test protocol",
    ))

    comparison_protocol = commands.add_parser(
        "comparison-protocol", help="comparison protocols",
    )
    comparison_protocol_commands = comparison_protocol.add_subparsers(
        dest="action", required=True,
    )
    _write_common(comparison_protocol_commands.add_parser(
        "create", help="create and freeze a comparison protocol",
    ))
    comparison_protocol_get = comparison_protocol_commands.add_parser(
        "get", help="read a comparison protocol",
    )
    _common(comparison_protocol_get)
    comparison_protocol_get.add_argument("id")

    feedback_get = feedback_commands.add_parser("get", help="read feedback")
    _common(feedback_get)
    feedback_get.add_argument("id")

    package = commands.add_parser("package", help="knowledge packages")
    package_commands = package.add_subparsers(dest="action", required=True)
    _write_common(package_commands.add_parser("build", help="build a package"))
    package_get = package_commands.add_parser("get", help="read a knowledge package")
    _common(package_get)
    package_get.add_argument("id")

    # -- read-only resource getters ----------------------------------------
    for name, label in (
        ("segment", "a session segment"),
        ("summary", "a performance summary"),
        ("comparison", "a performance comparison"),
        ("recommendation", "a recommendation version"),
        ("diagnosis", "a diagnosis"),
    ):
        reader = commands.add_parser(name, help=f"read {label}")
        reader_commands = reader.add_subparsers(dest="action", required=True)
        getter = reader_commands.add_parser("get", help=f"read {label}")
        _common(getter)
        getter.add_argument("id")

    return parser


# --- helpers ----------------------------------------------------------------


def _user_scope(args: argparse.Namespace) -> str:
    value = args.user_scope or os.environ.get("GAME_APP_USER_SCOPE_ID")
    if not value:
        raise ContractError(
            ErrorCode.SESSION_CONTEXT_INVALID,
            "no local user scope is configured; pass --user-scope or set"
            " GAME_APP_USER_SCOPE_ID",
        )
    if not _ID_RE.match(value):
        # The message never echoes the value: a flag is not a secret, but it
        # must not end up in an error envelope or a log line.
        raise ContractError(
            ErrorCode.SESSION_CONTEXT_INVALID,
            "the local user scope is not a valid 32-character lowercase hex id",
        )
    return value


def _request_id(args: argparse.Namespace) -> str:
    value = args.request_id
    if value is None:
        return uuid.uuid4().hex
    if not _ID_RE.match(value):
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            "the request id must be a 32-character lowercase hex id",
        )
    return value


def _mutation_context(args: argparse.Namespace) -> MutationContext:
    if not args.key:
        raise ContractError(
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
            "a write command needs --key; the same key and content replay the"
            " original response",
        )
    return MutationContext(
        user_scope_id=_user_scope(args),
        idempotency_key=args.key,
        request_id=_request_id(args),
    )


def _typed_document(model: Any, args: argparse.Namespace) -> Any:
    """Validate the caller's JSON into the contract model the owner declares.

    Typing happens here so a malformed document never reaches a domain owner as
    a half-valid dict, and never escapes as a pydantic traceback.
    """
    try:
        # JSON-mode validation, not Python mode: the CLI is a JSON boundary and
        # must accept exactly what an HTTP caller's body would (enums as their
        # string values, no Python-only coercions).
        return model.model_validate_json(canonical_json(_load_json(args.input)))
    except ValidationError as error:
        locations = sorted({
            ".".join(str(part) for part in item["loc"]) for item in error.errors()
        })
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            "the input does not satisfy the request contract: "
            + ", ".join(locations),
        ) from error


def _load_json(source: str) -> Any:
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(
            encoding="utf-8"
        )
    except OSError as error:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            "the input file cannot be read",
        ) from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ContractError(
            ErrorCode.MALFORMED_REQUEST, "the input is not valid JSON",
        ) from error


def _files_root(args: argparse.Namespace) -> str:
    return args.files or os.environ.get("GAME_APP_FILES") or DEFAULT_FILES


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db or os.environ.get("GAME_APP_DB") or DEFAULT_DB)


def _repository(args: argparse.Namespace) -> ProductRepository:
    repository = ProductRepository(_db_path(args))
    repository.migrate()
    return repository


def _application(args: argparse.Namespace, repository: ProductRepository) -> Application:
    # No provider is configured from the CLI: a remote model is never called
    # implicitly, and a task needing one reports that state honestly.
    probe = None
    if getattr(args, "probe", "none") == "windows":
        # Only the packaged read-only probe; it is opt-in because real Windows
        # evidence (G-WIN) is not claimed by default.
        probe = WindowsProbeAdapter()
    return Application(repository, file_root=_files_root(args), probe=probe)


def _is_worker(args: argparse.Namespace) -> bool:
    """Commands that advance the execution slot need the single-writer lock.

    Reads, registrations and short control requests are deliberately not
    locked: they must complete while a long task is running (07 §3).
    """
    if args.command == "task" and args.action == "run":
        return True
    return bool(getattr(args, "wait", False))


def _plain(value: Any) -> Any:
    """Turn a domain result into JSON-safe data without guessing at fields."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in fields(value)
        }
    return value


def _dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        # A domain result such as PackageBuildResult: print its declared fields,
        # never a repr that could leak a private path.
        printed = _plain(value)
        if isinstance(printed, dict) and printed:
            return printed
    if hasattr(value, "task"):
        return {
            "task": value.task.model_dump(mode="json"),
            "error": None if value.error is None else value.error.model_dump(mode="json"),
            "result_refs": [
                ref.model_dump(mode="json") for ref in value.result_refs
            ],
            "progress": {
                "phase": value.progress.phase,
                "last_activity_at": value.progress.last_activity_at,
                "completed_steps": list(value.progress.completed_steps),
            },
        }
    raise TypeError(f"the CLI cannot print {type(value)!r}")


def _emit(value: Any) -> None:
    sys.stdout.write(canonical_json(_dump(value)).decode("utf-8") + "\n")
    sys.stdout.flush()


def _wait(application: Application, accepted: Any, user_scope_id: str) -> Any:
    """Run until the accepted task is terminal, then print its own view.

    ``run_once`` claims the oldest queued task, which may not be the one just
    accepted; stopping after a single run would print a still-queued view.  A
    task that cannot be claimed (the slot is held by a capture) also ends the
    wait, and its honest state is what gets printed.
    """
    for _ in range(_WAIT_LIMIT):
        outcome = application.run_once(user_scope_id=user_scope_id)
        if outcome is None or outcome.task_id == accepted.task_id:
            break
    return application.get_task(accepted.task_id, user_scope_id)


# Every command/action pair that changes state.  A pair absent from this set is
# a read: it must not require a key and must not take the worker lock.
_WRITE_ACTIONS = {
    ("feedback", "record"),
    ("next-action", "plan"),
    ("preference", "declare"),
    ("target-acceptance", "record"),
    ("outcome", "record"),
    ("recommend", None),
    ("consume-impact", None),
    ("task", "cancel"),
    ("task", "resume"),
    ("game", "select"),
    ("installation", "register"),
    ("local-setting", "register"),
    ("device", "scan"),
    ("file", "upload"),
    ("knowledge", "collect"),
    ("import", "run"),
    ("import", "declare-identity"),
    ("package", "build"),
    ("session", "create"),
    ("session", "end-segment"),
    ("session", "split"),
    ("session", "end"),
    ("session", "bind"),
    ("adopt", None),
    ("historical-configuration", "record"),
    ("answer", None),
    ("amendment", "create"),
    ("log", "associate"),
    ("analyze", None),
    ("compare", None),
    ("test-protocol", "create"),
    ("comparison-protocol", "create"),
}


def _needs_key(args: argparse.Namespace) -> bool:
    return (args.command, getattr(args, "action", None)) in _WRITE_ACTIONS


def _needs_scope(args: argparse.Namespace) -> bool:
    """Every caller-facing command needs a local scope; ``task run`` rules it in.

    ``task run`` advances the execution slot on the caller's behalf, so it needs
    a resolved scope even though it carries no idempotency key.
    """
    if _needs_key(args):
        return True
    return args.command == "task" and args.action == "run"


def _error_exit(error: ContractError, request_id: str) -> int:
    envelope = error.to_envelope(request_id)
    sys.stderr.write(
        canonical_json(envelope.model_dump(mode="json")).decode("utf-8") + "\n"
    )
    sys.stderr.flush()
    return _EXIT_BY_CATEGORY.get(error.category.value, 6)


def _run_one(application: Application, user_scope_id: str | None) -> dict[str, Any]:
    outcome = application.run_once(user_scope_id=user_scope_id)
    if outcome is None:
        return {"task_id": None, "state": None}
    return {"task_id": outcome.task_id, "state": outcome.state.value}


def dispatch(application: Application, args: argparse.Namespace) -> Any:
    """Call exactly one Application entry; the CLI adds no domain rule."""
    if args.command == "feedback" and args.action == "record":
        accepted = application.record_feedback(
            _load_json(args.input), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
        return _wait(application, accepted, _user_scope(args)) if args.wait else accepted
    if args.command == "next-action" and args.action == "plan":
        accepted = application.plan_next_action(
            _load_json(args.input), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
        return _wait(application, accepted, _user_scope(args)) if args.wait else accepted
    if args.command == "preference" and args.action == "declare":
        return application.declare_preference(
            _load_json(args.input), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "target-acceptance" and args.action == "record":
        return application.record_target_acceptance(
            _load_json(args.input), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "outcome" and args.action == "record":
        return application.record_outcome(
            _load_json(args.input), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "outcome" and args.action == "get":
        return application.get_outcome(args.id, _user_scope(args))
    if args.command == "recommend":
        accepted = application.recommend(
            _load_json(args.input), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
        return _wait(application, accepted, _user_scope(args)) if args.wait else accepted
    if args.command == "consume-impact":
        return application.consume_impact(args.notice_id, _mutation_context(args))
    if args.command == "task":
        if args.action == "get":
            return application.get_task(args.task_id, _user_scope(args))
        if args.action == "run":
            return _run_one(application, _user_scope(args))
        if args.action == "cancel":
            return application.cancel_task(args.task_id, _mutation_context(args))
        if args.action == "resume":
            return application.resume_task(
                _load_json(args.input), _mutation_context(args),
            )
    if args.command == "session":
        if args.action == "get":
            return application.get_session(args.id, _user_scope(args))
        if args.action == "create":
            return application.create_session(
                _typed_document(CreateSessionRequest, args), _mutation_context(args),
                request_schema_version=args.contract_version,
            )
        if args.action == "end-segment":
            return application.end_segment(
                _typed_document(EndSegmentRequest, args), _mutation_context(args),
                request_schema_version=args.contract_version,
            )
        if args.action == "split":
            return application.split_segment(
                _typed_document(SplitSegmentRequest, args), _mutation_context(args),
                request_schema_version=args.contract_version,
            )
        if args.action == "end":
            return application.end_session(
                _typed_document(EndSessionRequest, args), _mutation_context(args),
                request_schema_version=args.contract_version,
            )
        if args.action == "bind":
            return application.bind_configuration(
                _typed_document(BindConfigurationRequest, args), _mutation_context(args),
                request_schema_version=args.contract_version,
            )
    if args.command == "game":
        if args.action == "list":
            return application.list_games(
                GameCatalogQuery(
                    query=args.query, cursor=args.cursor, limit=args.limit,
                ),
                _user_scope(args),
            )
        if args.action == "select":
            return application.select_game(
                _typed_document(GameSelectionRequest, args), _mutation_context(args),
                request_schema_version=args.contract_version,
            )
    if args.command == "installation":
        if args.action == "list":
            return application.list_installations(
                InstallationCatalogQuery(
                    game_id=args.game_id, cursor=args.cursor, limit=args.limit,
                ),
                _user_scope(args),
            )
        if args.action == "register":
            return application.create_installation(
                _typed_document(CreateInstallationRequest, args), _mutation_context(args),
                request_schema_version=args.contract_version,
            )
    if args.command == "local-setting":
        if args.action == "list":
            return application.list_local_settings(
                args.installation_id, _user_scope(args),
            )
        if args.action == "register":
            return application.register_local_setting(
                _typed_document(RegisterLocalSettingRequest, args), _mutation_context(args),
                request_schema_version=args.contract_version,
            )
    if args.command == "device" and args.action == "scan":
        accepted = application.scan_device(
            _typed_document(DeviceScanInput, args), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
        return _wait(application, accepted, _user_scope(args)) if args.wait else accepted
    if args.command == "file" and args.action == "upload":
        return _upload(application, args)
    if args.command == "knowledge" and args.action == "collect":
        accepted = application.collect_knowledge(
            _typed_document(KnowledgeInput, args), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
        return _wait(application, accepted, _user_scope(args)) if args.wait else accepted
    if args.command == "import" and args.action == "declare-identity":
        return application.declare_import_identity(
            _typed_document(ImportIdentityMappingRequest, args), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "import" and args.action == "run":
        accepted = application.register_task(
            _typed_document(KnowledgeImportInput, args), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
        return _wait(application, accepted, _user_scope(args)) if args.wait else accepted
    if args.command == "package" and args.action == "build":
        return _build_package(application, args)
    if args.command == "package":
        return application.get_package(args.id, _user_scope(args))
    if args.command == "adopt":
        return application.adopt_configuration(
            _typed_document(AdoptConfigurationInput, args), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "historical-configuration" and args.action == "record":
        return application.record_historical_configuration(
            _typed_document(HistoricalConfigurationInput, args), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "answer":
        return application.answer_information(
            _typed_document(InformationResponseInput, args), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "amendment" and args.action == "create":
        return application.create_information_amendment(
            _typed_document(CreateInformationAmendmentRequest, args),
            _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "artifact" and args.action == "get":
        return application.get_artifact(args.id, _user_scope(args))
    if args.command == "log":
        if args.action == "inspect":
            return application.inspect_log(
                args.file_id, args.schema, _user_scope(args),
            )
        if args.action == "associate":
            return application.associate_log(
                _typed_document(LogAssociationRequest, args), _mutation_context(args),
                request_schema_version=args.contract_version,
            )
    if args.command == "analyze":
        accepted = application.analyze(
            _typed_document(AnalysisInput, args), _mutation_context(args),
        )
        return _wait(application, accepted, _user_scope(args)) if args.wait else accepted
    if args.command == "compare":
        return application.compare(
            _typed_document(CompareInput, args), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "test-protocol" and args.action == "create":
        return application.create_test_protocol(
            _typed_document(CreateTestProtocolRequest, args), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
    if args.command == "comparison-protocol":
        if args.action == "create":
            return application.create_comparison_protocol(
                _typed_document(CreateComparisonProtocolRequest, args),
                _mutation_context(args),
                request_schema_version=args.contract_version,
            )
        if args.action == "get":
            return application.get_comparison_protocol(args.id, _user_scope(args))
    if args.command == "metric" and args.action == "list":
        return application.list_metric_definitions(
            MetricCatalogQuery(frame_stream_kind=args.frame_stream_kind)
        )
    if args.command == "feedback" and args.action == "get":
        return application.get_feedback(args.id, _user_scope(args))
    if args.command == "segment":
        return application.get_segment(args.id, _user_scope(args))
    if args.command == "summary":
        return application.get_summary(args.id, _user_scope(args))
    if args.command == "comparison":
        return application.get_comparison(args.id, _user_scope(args))
    if args.command == "recommendation":
        return application.get_recommendation(args.id, _user_scope(args))
    if args.command == "diagnosis":
        return application.get_diagnosis(args.id, _user_scope(args))
    raise ContractError(
        ErrorCode.INPUT_VALIDATION_FAILED, "no command entry matched the request",
    )


def _upload(application: Application, args: argparse.Namespace) -> dict[str, Any]:
    """Store one local file as a managed artifact.

    The bytes are read once from the caller's path; the id is derived from the
    user scope and idempotency key, so the same key reuses the same file.
    """
    context = _mutation_context(args)
    path = Path(args.path)
    if not path.is_file():
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            "the upload path is not an existing regular file",
        )
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED, "the upload file cannot be read",
        ) from error
    name = args.name or path.name
    file_id = application.files.store_artifact(
        data,
        relative_name=name,
        stable_key=f"upload:{context.user_scope_id}:{context.idempotency_key}",
        user_scope_id=context.user_scope_id,
    )
    stored = application.files.repository.get_object("ManagedFile", file_id)
    manifest = stored.payload["manifest"]
    return {
        "file_id": file_id,
        "relative_name": manifest["relative_name"],
        "byte_length": manifest["byte_length"],
        "sha256": manifest["sha256"],
    }


def _build_package(application: Application, args: argparse.Namespace) -> Any:
    """``build_package`` takes several fields, not one request document."""

    context = _mutation_context(args)
    document = _load_json(args.input)
    if not isinstance(document, Mapping):
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED, "the package input must be a JSON object",
        )
    missing = [
        key for key in ("import_snapshot_ids", "declared_scope", "game_context_id")
        if key not in document
    ]
    if missing:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            "the package input is missing " + ", ".join(missing),
        )
    return application.build_package(
        import_snapshot_ids=document["import_snapshot_ids"],
        prior_package_id=document.get("prior_package_id"),
        declared_scope=document["declared_scope"],
        game_context_id=document["game_context_id"],
        user_scope_id=context.user_scope_id,
        idempotency_key=context.idempotency_key,
        request_schema_version=args.contract_version,
        image_required=bool(document.get("image_required", False)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stdout)
        return 0
    # The envelope needs one valid correlation id even when the flag is bad;
    # the raw flag value is never echoed back or placed in the envelope.
    request_id = uuid.uuid4().hex
    repository = None
    lock: ApplicationLock | None = None
    try:
        request_id = _request_id(args)
        # Resolve the scope and the key before opening the database: a missing
        # local scope or key is a pure validation failure with no write, and a
        # malformed flag is a contract error, not an internal one.
        if _needs_key(args):
            _mutation_context(args)
        elif _needs_scope(args) or args.user_scope or os.environ.get(
            "GAME_APP_USER_SCOPE_ID"
        ):
            _user_scope(args)
        # 07 §1: the worker slot is exclusive.  The lock sits beside the
        # database it protects, so two workers on one database cannot both run
        # even when they were given different managed-file roots.  A second
        # worker gets a stable storage error; a crash releases the lock with
        # its handle.
        if _is_worker(args):
            lock = ApplicationLock(_db_path(args).parent).acquire()
        repository = _repository(args)
        application = _application(args, repository)
        _emit(dispatch(application, args))
        return 0
    except ContractError as error:
        return _error_exit(error, request_id)
    except Exception:  # noqa: BLE001 - never leak a traceback to the caller
        return _error_exit(
            ContractError(
                ErrorCode.INTERNAL_ERROR,
                "the command failed unexpectedly; read the task or retry the"
                " same submission",
            ),
            request_id,
        )
    finally:
        if repository is not None:
            repository.close()
        if lock is not None:
            lock.release()


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())


__all__ = ["main", "build_parser", "dispatch", "CONTRACT_VERSION"]
