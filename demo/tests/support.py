"""Shared demo test harness. All state lives in temporary directories."""

from __future__ import annotations

import contextlib
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from demo.app.contracts import Command, Reply, Scope
from demo.app.service import create_service

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DEFAULT_SCENARIO = "mw2-rtx4060"


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone.utc)
        self._monotonic = 1000.0

    def now_iso(self) -> str:
        return self._now.isoformat(timespec="seconds")

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
        self._monotonic += seconds


class DemoHarness:
    def __init__(self, *, fixture_root: Path | None = None, clock: FakeClock | None = None):
        self._temp = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self._temp.name)
        self.clock = clock or FakeClock()
        self.service = create_service(
            self.runtime_root,
            fixture_root=Path(fixture_root or FIXTURES),
            clock=self.clock,
        )
        self.last_command: Command | None = None
        self.commands: list[Command] = []

    def command(
        self,
        action: str,
        payload: dict | None = None,
        *,
        scenario: str = DEFAULT_SCENARIO,
        request_id: str | None = None,
        epoch: int | None = None,
        expected_revision: int | None = None,
    ) -> Reply:
        workspace = self.workspace(scenario)
        chosen_epoch = epoch if epoch is not None else workspace["workspace_epoch"]
        chosen_revision = (
            expected_revision if expected_revision is not None else workspace["revision"]
        )
        command = Command(
            request_id=request_id or uuid.uuid4().hex,
            scenario_id=scenario,
            workspace_epoch=chosen_epoch,
            expected_revision=chosen_revision,
            action=action,
            payload=payload or {},
        )
        self.last_command = command
        self.commands.append(command)
        return self.service.command(command)

    def workspace(self, scenario: str = DEFAULT_SCENARIO) -> dict:
        return self.service.workspace(scenario)

    def entity(self, entity_id: str, *, scenario: str = DEFAULT_SCENARIO) -> dict:
        workspace = self.workspace(scenario)
        return self.service.entity(Scope(scenario, workspace["workspace_epoch"]), entity_id)

    def task(self, task_id: str, *, scenario: str = DEFAULT_SCENARIO) -> dict:
        workspace = self.workspace(scenario)
        return self.service.task(Scope(scenario, workspace["workspace_epoch"]), task_id)

    def run(self, task_id: str, *, scenario: str = DEFAULT_SCENARIO, max_steps: int = 100) -> dict:
        for _ in range(max_steps):
            task = self.task(task_id, scenario=scenario)
            if task["status"] not in ("queued", "running"):
                return task
            self.clock.advance(1.0)
            self.service.tasks.tick()
        raise AssertionError(f"task {task_id} did not settle within {max_steps} steps")

    def knowledge(
        self,
        *,
        scenario: str = DEFAULT_SCENARIO,
        mode: str = "discover",
        budget: dict | None = None,
        source_ids: list[str] | None = None,
    ) -> str:
        payload = {"mode": mode}
        if budget is not None:
            payload["budget"] = budget
        if source_ids is not None:
            payload["source_ids"] = source_ids
        reply = self.command("knowledge.collect", payload, scenario=scenario)
        task = self.run(reply.body["task_id"], scenario=scenario)
        return task["result"]["knowledge_id"]

    def recommendation(
        self, *, scenario: str = DEFAULT_SCENARIO, knowledge_id: str | None = None
    ) -> str:
        knowledge_id = knowledge_id or self.knowledge(scenario=scenario)
        context_id = self.workspace(scenario)["context_id"]
        reply = self.command(
            "recommendation.generate",
            {"context_id": context_id, "knowledge_id": knowledge_id},
            scenario=scenario,
        )
        return self.ref(reply, "Recommendation")

    def adopt(
        self, *, scenario: str = DEFAULT_SCENARIO, recommendation_id: str | None = None
    ) -> str:
        recommendation_id = recommendation_id or self.recommendation(scenario=scenario)
        recommendation = self.entity(recommendation_id, scenario=scenario)
        baseline = self.service.catalog.get(scenario)["templates"]["baseline"]["values"]
        a0 = self.command(
            "configuration.adopt",
            {
                "origin": "baseline",
                "recommendation_id": None,
                "expected_current_id": None,
                "settings": baseline,
                "preset": "baseline",
                "confirmation": "这是演示原配置（玩家声明）",
            },
            scenario=scenario,
        )
        a0_id = self.ref(a0, "AppliedConfiguration")
        settings = {
            item["setting_id"]: item["value"] for item in recommendation["settings"]
        }
        a1 = self.command(
            "configuration.adopt",
            {
                "origin": "recommendation",
                "recommendation_id": recommendation_id,
                "expected_current_id": a0_id,
                "settings": settings,
                "preset": recommendation["template"],
                "confirmation": "我已手动设置（玩家声明）",
            },
            scenario=scenario,
        )
        return self.ref(a1, "AppliedConfiguration")

    def session(
        self,
        *,
        scenario: str = DEFAULT_SCENARIO,
        applied_id: str | None = None,
        plan_id: str | None = None,
        run_conditions: dict | None = None,
    ) -> dict:
        context_id = self.workspace(scenario)["context_id"]
        reply = self.command(
            "session.create",
            {
                "context_id": context_id,
                "applied_id": applied_id,
                "plan_id": plan_id,
                "run_conditions": run_conditions or {},
            },
            scenario=scenario,
        )
        segment = self.entity(self.ref(reply, "Segment"), scenario=scenario)
        binding = self.entity(self.ref(reply, "Binding"), scenario=scenario)
        return {
            "session_id": self.ref(reply, "Session"),
            "segment_id": segment["id"],
            "binding_id": binding["id"],
            "segment_revision": segment["revision"],
            "binding_revision": binding["revision"],
        }

    def summary(
        self,
        label: str = "recommended",
        *,
        scenario: str = DEFAULT_SCENARIO,
        applied_id: str | None = None,
        plan_id: str | None = None,
        run_conditions: dict | None = None,
    ) -> str:
        if applied_id is None:
            applied_id = self.adopt(scenario=scenario)
        session = self.session(
            scenario=scenario,
            applied_id=applied_id,
            plan_id=plan_id,
            run_conditions=run_conditions
            if run_conditions is not None
            else {"scene": "演示场景", "note": "测试夹具声明"},
        )
        reply = self.command(
            "capture.start",
            {
                "session_id": session["session_id"],
                "segment_id": session["segment_id"],
                "expected_segment_revision": session["segment_revision"],
                "expected_binding_revision": session["binding_revision"],
                "label": label,
            },
            scenario=scenario,
        )
        task = self.run(reply.body["task_id"], scenario=scenario)
        artifact_id = task["result"]["artifact_id"]
        segment = self.entity(session["segment_id"], scenario=scenario)
        binding = self.entity(segment["binding_id"], scenario=scenario)
        analyzed = self.command(
            "performance.analyze",
            {
                "artifact_id": artifact_id,
                "segment_id": segment["id"],
                "segment_revision": segment["revision"],
                "binding_revision": binding["revision"],
            },
            scenario=scenario,
        )
        return self.ref(analyzed, "Summary")

    def story(self, *, scenario: str = DEFAULT_SCENARIO, accepted: bool = True) -> dict:
        catalog = self.service.catalog.get(scenario)
        rules = catalog["rules"]
        rule = rules["symptom_rules"][0]
        protected = [rules["protected_options"][0]]
        a1 = self.adopt(scenario=scenario)
        a0 = self.entity(a1, scenario=scenario)["parent_id"]
        plan_id = self.ref(
            self.command(
                "plan.save",
                {
                    "scene": catalog["context"]["scene_tag"],
                    "duration_s": 60,
                    "repeats": 1,
                },
                scenario=scenario,
            ),
            "TestPlan",
        )
        s1 = self.summary("recommended", scenario=scenario, applied_id=a1, plan_id=plan_id)
        s1_entity = self.entity(s1, scenario=scenario)
        protocol = self.ref(
            self.command(
                "comparison.freeze",
                {
                    "mode": "fixed",
                    "threshold_ms": 50.0,
                    "baseline_summary_ids": [s1],
                    "exploratory": False,
                },
                scenario=scenario,
            ),
            "ComparisonProtocol",
        )
        saved = self.command(
            "feedback.save",
            {
                "session_id": s1_entity["session_id"],
                "summary_id": s1,
                "satisfied": False,
                "symptoms": list(rule["symptoms"]),
                "severity": "occasional",
                "raw_text": "交战时偶尔卡一下，希望保持画面清晰",
                "protected_setting_ids": protected,
            },
            scenario=scenario,
        )
        next_step = self.command(
            "feedback.next",
            {
                "feedback_id": self.ref(saved, "Feedback"),
                "applied_id": a1,
                "summary_id": s1,
            },
            scenario=scenario,
        )
        change_set = self.run(next_step.body["task_id"], scenario=scenario)["result"]["change_set_id"]
        changes_entity = self.entity(change_set, scenario=scenario)
        changes = {
            setting_id: change["to"]
            for setting_id, change in changes_entity["changes"].items()
        }
        a2 = self.ref(
            self.command(
                "configuration.edit",
                {
                    "expected_current_id": a1,
                    "changes": changes,
                    "change_set_id": change_set,
                    "confirmation": "我已手动调整",
                },
                scenario=scenario,
            ),
            "AppliedConfiguration",
        )
        s2 = self.summary("adjusted", scenario=scenario, applied_id=a2, plan_id=plan_id)
        comparison = self.ref(
            self.command(
                "comparison.run",
                {
                    "protocol_id": protocol,
                    "before_summary_id": s1,
                    "after_summary_id": s2,
                },
                scenario=scenario,
            ),
            "Comparison",
        )
        outcome = self.ref(
            self.command(
                "outcome.save",
                {
                    "change_set_id": change_set,
                    "applied_id": a2,
                    "comparison_id": comparison,
                    "accepted": accepted,
                    "reason": "演示结果接受" if accepted else "画面不接受，恢复原方案",
                },
                scenario=scenario,
            ),
            "Outcome",
        )
        story = {
            "a0_id": a0,
            "a1_id": a1,
            "a2_id": a2,
            "s1_id": s1,
            "s2_id": s2,
            "protocol_id": protocol,
            "change_set_id": change_set,
            "comparison_id": comparison,
            "outcome_id": outcome,
        }
        if not accepted:
            story["rollback_id"] = self.ref(
                self.command(
                    "configuration.rollback",
                    {
                        "expected_current_id": a2,
                        "target_id": a1,
                        "confirmation": "我已手动恢复",
                    },
                    scenario=scenario,
                ),
                "AppliedConfiguration",
            )
        return story

    def reopen(self) -> None:
        """Restart the service against the same runtime directory and clock."""
        self.service.close()
        self.service = create_service(
            self.runtime_root, fixture_root=FIXTURES, clock=self.clock
        )

    def ref(self, reply: Reply, kind: str) -> str:
        matches = [ref["id"] for ref in reply.body["refs"] if ref.get("kind") == kind]
        if len(matches) != 1:
            raise AssertionError(f"expected exactly one {kind} reference, found {len(matches)}")
        return matches[0]

    def upload(
        self,
        data: bytes,
        media_type: str = "text/csv",
        *,
        scenario: str = DEFAULT_SCENARIO,
        filename: str = "sample.csv",
    ) -> str:
        import hashlib

        workspace = self.workspace(scenario)
        command = Command(
            request_id=uuid.uuid4().hex,
            scenario_id=scenario,
            workspace_epoch=workspace["workspace_epoch"],
            expected_revision=workspace["revision"],
            action="file.upload",
            payload={
                "media_type": media_type,
                "filename": filename,
                "size": len(data),
                "content_hash": hashlib.sha256(data).hexdigest(),
            },
        )
        self.last_command = command
        self.commands.append(command)
        reply = self.service.upload(
            command, data=data, media_type=media_type, filename=filename
        )
        return self.ref(reply, "File")

    def close(self) -> None:
        try:
            self.service.close()
        finally:
            self._temp.cleanup()


class DemoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = DemoHarness()

    def tearDown(self) -> None:
        self.h.close()


@contextlib.contextmanager
def running_http(service):
    from demo.app.http import create_http_server

    server = create_http_server(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
