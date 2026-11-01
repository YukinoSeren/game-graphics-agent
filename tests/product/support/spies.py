"""Counting provider stub: no network, explicit unknown/timeout behaviour."""

from __future__ import annotations

import threading
from typing import Any, Callable, Mapping


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
            raise TimeoutError("simulated provider timeout")
        return {
            "provider_request_id": f"stub-{self.sends}",
            "returned_model": "stub-model-1",
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }
