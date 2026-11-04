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
from game_app.knowledge import IdentityService, KnowledgeService
from game_app.performance import PerformanceService
from game_app.recommendation import RecommendationService, ReviewService
from game_app.repository import ProductRepository
from game_app.tasks import DEFAULT_ROUTES, TaskService


class Application:
    def __init__(
        self,
        repository: ProductRepository,
        *,
        file_root: str,
        provider: Any | None = None,
        probe: Any | None = None,
        capture_registry: Any | None = None,
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
        self.knowledge = KnowledgeService(repository, self.files, clock=clock)
        # Registered only now that a real handler exists: a missing
        # implementation must never masquerade as an empty success.
        self.tasks.handlers["knowledge"] = self.knowledge.handle
        self.performance = PerformanceService(
            repository, self.files, clock=clock, tasks=self.tasks,
            capture_registry=capture_registry,
        )
        # Analysis is a durable domain task; the entry pre-validates before the
        # key is consumed, and the handler runs once the task is claimed.
        self.tasks.handlers["analysis"] = self.performance.handle_analysis
        if capture_registry is not None:
            # Without a registry the type stays unregistered and is refused at
            # registration instead of running an empty success.
            self.tasks.handlers["capture"] = self.performance.handle_capture
        self.recommendations = RecommendationService(
            repository,
            self.files,
            self.knowledge,
            adoption=self.adoption,
            performance=self.performance,
            clock=clock,
        )
        self.review = ReviewService(repository, self.knowledge, clock=clock)
        # 03's consumer exists: the impact notice gets a real host.
        self.tasks.handlers["recommendation"] = self.recommendations.handle

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
        interrupted = self.tasks.recover_on_startup()
        if self.performance.capture_registry is not None:
            # Only a pid + start time + native name this task can prove is
            # stopped; anything else becomes unresolved and blocks a new launch.
            self.performance.reconcile_capture_leases()
        return interrupted

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

    # -- 02 knowledge entries ----------------------------------------------
    def collect_knowledge(self, task_input: Any, context: MutationContext, **kwargs: Any):
        """Register a knowledge task; pure conflicts do not consume the key.

        A registered key is replayed before these prechecks: a retry must return
        the original acceptance (or an IDEMPOTENCY_CONFLICT) even when the
        world changed since (error §4.2).
        """
        registered = self.repository.find_idempotency(
            context.user_scope_id, "POST", DEFAULT_ROUTES["knowledge"],
            context.idempotency_key,
        )
        if registered is None:
            self.knowledge.precheck_collection(
                task_input, user_scope_id=context.user_scope_id
            )
        return self.register_task(task_input, context, **kwargs)

    def build_package(self, *args: Any, **kwargs: Any):
        return self.knowledge.build_package(*args, **kwargs)

    def get_package(self, package_id: str, user_scope_id: str, **kwargs: Any):
        return self.knowledge.get_package(
            package_id, user_scope_id=user_scope_id, **kwargs
        )

    def notify_impacts(
        self, old_package_id: str, new_package_id: str,
        context: MutationContext, **kwargs: Any,
    ):
        """Register an impact notice; ``consume_impact`` is its 03 consumer."""
        return self.knowledge.notify_impacts(
            old_package_id,
            new_package_id,
            user_scope_id=context.user_scope_id,
            idempotency_key=context.idempotency_key,
            request_id=context.request_id,
            **kwargs,
        )

    # -- 03 recommendation entries -----------------------------------------
    def recommend(self, task_input: Any, context: MutationContext, **kwargs: Any):
        """Register a recommendation task; pure conflicts keep the key.

        The registered key is replayed before the prechecks, exactly as
        ``collect_knowledge`` does (error §4.2).
        """
        registered = self.repository.find_idempotency(
            context.user_scope_id, "POST", DEFAULT_ROUTES["recommendation"],
            context.idempotency_key,
        )
        if registered is None:
            self.recommendations.precheck(
                task_input, user_scope_id=context.user_scope_id
            )
        return self.register_task(task_input, context, **kwargs)

    def get_recommendation(self, recommendation_id: str, user_scope_id: str):
        return self.recommendations.get_recommendation(
            recommendation_id, user_scope_id=user_scope_id
        )

    def consume_impact(self, notice_id: str, context: MutationContext):
        return self.review.consume_impact(
            notice_id, user_scope_id=context.user_scope_id
        )

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

    def inspect_log(self, file_id: str, source_schema: str, user_scope_id: str):
        return self.performance.inspect_log(file_id, source_schema, user_scope_id)

    def associate_log(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.associate_log(request, context, **kwargs)

    def analyze(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.analyze(request, context)

    def get_summary(self, summary_id: str, user_scope_id: str):
        return self.performance.get_summary(summary_id, user_scope_id)

    def create_comparison_protocol(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.create_comparison_protocol(request, context, **kwargs)

    def compare(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.compare(request, context, **kwargs)

    def get_comparison(self, comparison_id: str, user_scope_id: str):
        return self.performance.get_comparison(comparison_id, user_scope_id)

    def get_comparison_protocol(self, protocol_id: str, user_scope_id: str):
        return self.performance.get_comparison_protocol(protocol_id, user_scope_id)

    def capture(self, request: Any, context: MutationContext):
        return self.performance.capture(request, context)

    def stop_capture(self, request: Any, context: MutationContext, **kwargs: Any):
        return self.performance.stop_capture(request, context, **kwargs)

    def get_capture_lease(self, task_id: str, user_scope_id: str):
        return self.performance.get_capture_lease(task_id, user_scope_id)

    def get_session(self, session_id: str, user_scope_id: str):
        return self.performance.get_session(session_id, user_scope_id)

    def get_segment(self, segment_id: str, user_scope_id: str):
        return self.performance.get_segment(segment_id, user_scope_id)


__all__ = ["Application"]
