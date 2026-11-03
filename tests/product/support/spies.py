"""Counting provider stub and the fault-injection boundaries (VAL-T02).

Neither helper performs I/O: the provider is an in-process stub and the crash
injector refuses to fire unless the repository is inside the caller's temporary
root (never the player's product database, never ``runs/``).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Mapping


class InjectedCrash(Exception):
    """A deliberate crash raised only at a named publishing boundary."""


class SpyProvider:
    def __init__(
        self,
        *,
        mode: str = "ok",
        on_send: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.mode = mode
        self.on_send = on_send
        self.sends = 0
        # True for the modes that model "the provider did run, the caller did
        # not get a usable answer"; a local retry must not increase sends.
        self.executed = False
        self.requests: list[Mapping[str, Any]] = []
        self._lock = threading.Lock()

    def send(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.sends += 1
        self.requests.append(request)
        if self.on_send is not None:
            self.on_send(request)
        if self.mode == "unknown":
            # Simulates "the request may have reached the provider".
            self.executed = True
            raise TimeoutError("simulated provider timeout")
        if self.mode == "lost":
            # The provider executed; the response was lost after execution.
            self.executed = True
            raise RuntimeError("simulated response loss after execution")
        if self.mode == "boom":
            # A wrapper exception that proves nothing about delivery.
            raise RuntimeError("simulated adapter wrapper failure")
        if self.mode == "reject":
            from game_app.tasks import ProviderRejected

            # An explicit provider response proving the request did not run.
            raise ProviderRejected("simulated provider rejection")
        self.executed = True
        return {
            "provider_request_id": f"stub-{self.sends}",
            "returned_model": "stub-model-1",
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }


class CrashInjector:
    """Raise at exactly one named publishing boundary, only on a test database.

    ``before_write`` and ``after_rename`` run before the database transaction;
    ``before_commit`` runs just before it.  The guard rejects a repository that
    is not inside ``guard_root`` or that lives under ``runs/``.
    """

    def __init__(self, boundary: str, *, guard_root: str | Path | None = None) -> None:
        if boundary not in {"before_write", "after_rename", "before_commit"}:
            raise ValueError("unknown crash boundary")
        self.boundary = boundary
        self.guard_root = None if guard_root is None else Path(guard_root).resolve()
        self.fired = 0

    def guard(self, repository_path: str | Path) -> None:
        if self.guard_root is None:
            raise InjectedCrash("the crash injector needs an explicit temporary root")
        path = Path(repository_path).resolve()
        if "runs" in path.parts:
            raise InjectedCrash("the crash injector never touches runs/")
        if path != self.guard_root and self.guard_root not in path.parents:
            raise InjectedCrash("the crash injector only touches its temporary root")

    def hook(self, boundary: str) -> Callable[..., None]:
        if boundary not in {"before_write", "after_rename", "before_commit"}:
            raise ValueError("unknown crash boundary")

        def fire(*_args: Any, **_kwargs: Any) -> None:
            if boundary == self.boundary:
                self.fired += 1
                raise InjectedCrash(f"injected crash at {boundary}")

        return fire


__all__ = ["CrashInjector", "InjectedCrash", "SpyProvider"]
