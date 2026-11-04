"""Stable demo-only command/reply types shared by the service layers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

JsonObject = dict[str, Any]


class Clock(Protocol):
    def now_iso(self) -> str: ...

    def monotonic(self) -> float: ...


class RealClock:
    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class Scope:
    scenario_id: str
    workspace_epoch: int


@dataclass(frozen=True)
class Command:
    request_id: str
    scenario_id: str
    workspace_epoch: int
    expected_revision: int
    action: str
    payload: JsonObject


@dataclass(frozen=True)
class DomainResult:
    status: int = 200
    refs: list[JsonObject] = field(default_factory=list)
    task_id: str | None = None
    data: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class Reply:
    status: int
    body: JsonObject


class DemoError(Exception):
    """A user-facing demo failure with a stable code and recovery hints."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        field_errors: list[JsonObject] | None = None,
        recovery_actions: list[str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field_errors = field_errors or []
        self.recovery_actions = recovery_actions or []

    def to_body(self) -> JsonObject:
        return {
            "demo_only": True,
            "error": {
                "code": self.code,
                "message": self.message,
                "field_errors": self.field_errors,
                "recovery_actions": self.recovery_actions,
            },
        }


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
