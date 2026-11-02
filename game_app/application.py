"""Application coordination layer.

Owns no domain rules: it wires the durable task service and the implemented
domain services to callers.  HTTP/CLI adapters must call this layer, never the
repository or a domain module directly.  A task type without a registered
handler is refused at registration instead of running as an empty success.
"""

from __future__ import annotations

from typing import Any, Mapping

from game_app.adoption import AdoptionService
from game_app.answers import AmendmentService, AnswerService
from game_app.contracts.common import Budget, MutationContext
from game_app.contracts.primitives import utc_now
from game_app.device import DeviceService
from game_app.errors import ContractError, ErrorCode, FieldError
from game_app.files import FileService
from game_app.importer import ImportService
from game_app.knowledge import IdentityService
from game_app.performance import PerformanceService
from game_app.repository import ProductRepository
from game_app.tasks import TaskService


class Application:
    def __init__(
        self,
        repository: ProductRepository,
        *,
        file_root: str,
        provider: Any | None = None,
        probe: Any | None = None,
        clock: Any = utc_now,
    ) -> None:
        # A managed-file root is required: provider calls must persist their
        # original response before a call may be recorded as succeeded.
        self.repository = repository
        self.files = FileService(repository, file_root)
        self.tasks = TaskService(
            repository, provider=provider, files=self.files, clock=clock
        )
        self.imports = ImportService(repository, self.files)
        self.tasks.handlers["knowledge_import"] = self.imports.handle
        self.devices = None
        if probe is not None:
            # Without an adapter the type stays unregistered: it is refused
            # rather than executed as an empty success.
            self.devices = DeviceService(repository, self.files, probe)
            self.tasks.handlers["device_scan"] = self.devices.handle
        self.adoption = AdoptionService(repository, self.files, clock=clock)
        self.answers = AnswerService(
            repository, self.files, tasks=self.tasks, clock=clock
        )
        self.amendments = AmendmentService(repository, self.files, clock=clock)
        self.identity = IdentityService(repository, self.files)
        self.performance = PerformanceService(repository, self.files, clock=clock)

    # -- task control -------------------------------------------------------
    def register_task(
        self,
        task_input: Any,
        context: MutationContext,
        *,
        request_schema_version: str = "product-contract/0.4",
        budget: Budget | None = None,
    ):
        kind = (
            task_input.get("kind")
            if isinstance(task_input, Mapping)
            else getattr(task_input, "kind", None)
        )
        if kind not in self.tasks.handlers:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "no handler is registered for this task type",
                field_errors=[FieldError(
                    path="kind",
                    code="enum",
                    message="no handler is registered for this task type",
                )],
            )
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

    # -- synchronous domain entries (thin wrappers, no extra rules) ---------
    def adopt_configuration(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.adoption.adopt_configuration(request, context, **kwargs)

    def record_historical_configuration(
        self, request: Any, context: MutationContext, **kwargs: Any
    ):
        return self.adoption.record_historical_configuration(request, context, **kwargs)

    def answer_information(self, response_input: Any, context: MutationContext, **kwargs: Any):
        return self.answers.answer_information(response_input, context, **kwargs)

    def create_information_amendment(
        self, request: Any, context: MutationContext, **kwargs: Any
    ):
        return self.amendments.create_information_amendment(request, context, **kwargs)

    def declare_import_identity(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.identity.declare_import_identity(request, context, **kwargs)

    # -- 04 performance entries (synchronous, thin wrappers) ----------------
    def create_session(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.create_session(request, context, **kwargs)

    def end_segment(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.end_segment(request, context, **kwargs)

    def split_segment(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.split_segment(request, context, **kwargs)

    def end_session(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.end_session(request, context, **kwargs)

    def bind_configuration(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.bind_configuration(request, context, **kwargs)

    def list_metric_definitions(self, query: Any):
        return self.performance.list_metric_definitions(query)

    def create_test_protocol(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.create_test_protocol(request, context, **kwargs)

    def inspect_log(self, file_id: str, source_schema: str):
        return self.performance.inspect_log(file_id, source_schema)

    def associate_log(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.associate_log(request, context, **kwargs)

    def get_session(self, session_id: str, user_scope_id: str):
        return self.performance.get_session(session_id, user_scope_id)

    def get_segment(self, segment_id: str, user_scope_id: str):
        return self.performance.get_segment(segment_id, user_scope_id)


__all__ = ["Application"]
