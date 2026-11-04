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
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence

from game_app.application import Application
from game_app.contracts.common import MutationContext
from game_app.errors import ContractError, ErrorCode
from game_app.repository import ProductRepository, canonical_json

CONTRACT_VERSION = "product-contract/0.4"
DEFAULT_DB = "product.sqlite3"
DEFAULT_FILES = "managed"

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

    for name, label in (
        ("session", "a play session"),
        ("segment", "a session segment"),
        ("summary", "a performance summary"),
        ("comparison", "a performance comparison"),
        ("package", "a knowledge package"),
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
        request_id=args.request_id or uuid.uuid4().hex,
    )


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


def _repository(args: argparse.Namespace) -> ProductRepository:
    path = args.db or os.environ.get("GAME_APP_DB") or DEFAULT_DB
    repository = ProductRepository(path)
    repository.migrate()
    return repository


def _application(args: argparse.Namespace, repository: ProductRepository) -> Application:
    root = args.files or os.environ.get("GAME_APP_FILES") or DEFAULT_FILES
    # No provider is configured from the CLI: a remote model is never called
    # implicitly, and a task needing one reports that state honestly.
    return Application(repository, file_root=root)


def _dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
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


def _wait(application: Application, accepted: Any) -> Any:
    application.run_once()
    return application.get_task(accepted.task_id)


def _error_exit(error: ContractError, request_id: str) -> int:
    envelope = error.to_envelope(request_id)
    sys.stderr.write(
        canonical_json(envelope.model_dump(mode="json")).decode("utf-8") + "\n"
    )
    sys.stderr.flush()
    return _EXIT_BY_CATEGORY.get(error.category.value, 6)


def _run_one(application: Application) -> dict[str, Any]:
    outcome = application.run_once()
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
        return _wait(application, accepted) if args.wait else accepted
    if args.command == "next-action" and args.action == "plan":
        accepted = application.plan_next_action(
            _load_json(args.input), _mutation_context(args),
            request_schema_version=args.contract_version,
        )
        return _wait(application, accepted) if args.wait else accepted
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
        return _wait(application, accepted) if args.wait else accepted
    if args.command == "consume-impact":
        return application.consume_impact(args.notice_id, _mutation_context(args))
    if args.command == "task":
        if args.action == "get":
            return application.get_task(args.task_id)
        if args.action == "run":
            return _run_one(application)
        if args.action == "cancel":
            return application.cancel_task(args.task_id, _mutation_context(args))
        if args.action == "resume":
            return application.resume_task(
                _load_json(args.input), _mutation_context(args),
            )
    if args.command == "session":
        return application.get_session(args.id, _user_scope(args))
    if args.command == "segment":
        return application.get_segment(args.id, _user_scope(args))
    if args.command == "summary":
        return application.get_summary(args.id, _user_scope(args))
    if args.command == "comparison":
        return application.get_comparison(args.id, _user_scope(args))
    if args.command == "package":
        return application.get_package(args.id, _user_scope(args))
    if args.command == "recommendation":
        return application.get_recommendation(args.id, _user_scope(args))
    if args.command == "diagnosis":
        return application.get_diagnosis(args.id, _user_scope(args))
    raise ContractError(
        ErrorCode.INPUT_VALIDATION_FAILED, "no command entry matched the request",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stdout)
        return 0
    request_id = args.request_id or uuid.uuid4().hex
    repository = None
    try:
        # Resolve the scope and the key before opening the database: a missing
        # local scope or key is a pure validation failure with no write.
        if args.command in {"consume-impact"} or (
            args.command == "task" and args.action in {"cancel", "resume"}
        ):
            _mutation_context(args)
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


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())


__all__ = ["main", "build_parser", "dispatch", "CONTRACT_VERSION"]
