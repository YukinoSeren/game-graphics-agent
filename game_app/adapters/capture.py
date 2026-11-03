"""Capture adapter boundary: a protocol, a registry and an inert stub.

No real capture tool is supported (G-GAME/G-FORMAT have no evidence), so this
module starts no process, elevates no privilege and stops no external session.
The only implementation is :class:`StubCaptureAdapter`, which records calls so
tests can prove that no second launch happened.

The registry is the capability gate: an adapter id/version that is not
registered **and verified** is refused by the capture entry with
``INPUT_VALIDATION_FAILED`` (04 §1, §5).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from game_app.contracts.primitives import utc_now


@dataclass(frozen=True)
class CaptureHandle:
    """What a launched tool session is known by, all from the backend."""

    native_name: str
    pid: int
    started_at: str


@dataclass(frozen=True)
class CaptureOutput:
    """The tool's output and its actual end; absence is explicit, never faked."""

    data: bytes | None
    ended_at: str | None
    reason: str | None = None


@runtime_checkable
class CaptureAdapter(Protocol):
    adapter_id: str
    adapter_version: str

    def start(self, *, native_name: str) -> CaptureHandle: ...

    def verify_ownership(
        self, *, native_name: str, pid: int, started_at: str
    ) -> bool: ...

    def stop(self, handle: CaptureHandle) -> CaptureOutput: ...


class CaptureAdapterRegistry:
    """Only an explicitly verified adapter may be used by the capture entry."""

    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], tuple[CaptureAdapter, bool]] = {}

    def register(self, adapter: CaptureAdapter, *, verified: bool = False) -> None:
        key = (adapter.adapter_id, adapter.adapter_version)
        if key in self._adapters:
            raise ValueError(f"adapter {key[0]}/{key[1]} is already registered")
        self._adapters[key] = (adapter, verified)

    def get(self, adapter_id: str, adapter_version: str) -> CaptureAdapter | None:
        entry = self._adapters.get((adapter_id, adapter_version))
        if entry is None or not entry[1]:
            return None
        return entry[0]

    def contains(self, adapter_id: str, adapter_version: str) -> bool:
        return (adapter_id, adapter_version) in self._adapters


class StubCaptureAdapter:
    """A test double: records launches/stops and never touches a real process."""

    adapter_id = "stub-capture"
    adapter_version = "1"

    def __init__(
        self,
        *,
        clock=utc_now,
        output: bytes | None = None,
        ownership_verifiable: bool = True,
    ) -> None:
        self.clock = clock
        self.output = output
        self.ownership_verifiable = ownership_verifiable
        self.starts = 0
        self.stops = 0
        self.handles: list[CaptureHandle] = []
        self._lock = threading.Lock()

    def start(self, *, native_name: str) -> CaptureHandle:
        with self._lock:
            self.starts += 1
            handle = CaptureHandle(
                native_name=native_name,
                pid=6000 + self.starts,
                started_at=self.clock(),
            )
            self.handles.append(handle)
        return handle

    def verify_ownership(
        self, *, native_name: str, pid: int, started_at: str
    ) -> bool:
        if not self.ownership_verifiable:
            return False
        return any(
            handle.native_name == native_name
            and handle.pid == pid
            and handle.started_at == started_at
            for handle in self.handles
        )

    def stop(self, handle: CaptureHandle) -> CaptureOutput:
        with self._lock:
            self.stops += 1
        return CaptureOutput(
            data=self.output,
            ended_at=self.clock(),
            reason=None if self.output is not None else "the tool produced no output",
        )


__all__ = [
    "CaptureAdapter",
    "CaptureAdapterRegistry",
    "CaptureHandle",
    "CaptureOutput",
    "StubCaptureAdapter",
]
