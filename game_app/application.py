"""Application coordination layer.

Owns no domain rules: it wires the durable task service (and, from ENG-T03, the
managed-file service) to callers.  HTTP/CLI adapters must call this layer, never
the repository or a domain module directly.
"""

from __future__ import annotations

from typing import Any

from game_app.contracts.common import Budget, MutationContext
from game_app.repository import ProductRepository
from game_app.tasks import TaskService


class Application:
    def __init__(self, repository: ProductRepository, *, provider: Any | None = None) -> None:
        self.repository = repository
        self.tasks = TaskService(repository, provider=provider)

    def register_task(
        self,
        task_input: Any,
        context: MutationContext,
        *,
        request_schema_version: str = "product-contract/0.4",
        budget: Budget | None = None,
    ):
        return self.tasks.register_task(
            task_input,
            context,
            request_schema_version=request_schema_version,
            budget=budget,
        )

    def get_task(self, task_id: str):
        return self.tasks.get_task(task_id)

    def cancel_task(self, task_id: str, context: MutationContext):
        return self.tasks.cancel_task(task_id, context)

    def resume_task(self, resume_input: Any, context: MutationContext):
        return self.tasks.resume_task(resume_input, context)

    def run_once(self):
        return self.tasks.run_once()

    def recover_on_startup(self) -> list[str]:
        return self.tasks.recover_on_startup()


__all__ = ["Application"]
