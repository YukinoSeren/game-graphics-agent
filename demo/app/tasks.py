"""Single-worker simulated task runner with recoverable stages."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable

from .contracts import DemoError, JsonObject, Scope

TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled", "interrupted", "superseded"}
PAUSED_STATUSES = {"waiting_input"}
RUNNING_STATUSES = {"queued", "running"}

Publish = Callable[[object, JsonObject, JsonObject], JsonObject | None]


class TaskRunner:
    def __init__(self, service):
        self.service = service
        self._definitions: dict[str, tuple[list[JsonObject], Publish]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- registration -------------------------------------------------
    def register(self, kind: str, phases: list[JsonObject], publish: Publish) -> None:
        if kind in self._definitions:
            raise RuntimeError(f"duplicate demo task registration: {kind}")
        if not phases:
            raise RuntimeError("a demo task needs at least one phase")
        for phase in phases:
            if not {"key", "label"} <= set(phase):
                raise RuntimeError("demo task phases need key and label")
        self._definitions[kind] = ([dict(phase) for phase in phases], publish)

    def definition(self, kind: str) -> tuple[list[JsonObject], Publish]:
        try:
            return self._definitions[kind]
        except KeyError as exc:
            raise DemoError("unknown_task_kind", "演示任务类型未注册。") from exc

    # -- enqueue ------------------------------------------------------
    def enqueue(
        self,
        tx,
        kind: str,
        input_data: JsonObject | None = None,
        *,
        parent_id: str | None = None,
        result: JsonObject | None = None,
    ) -> JsonObject:
        phases, _ = self.definition(kind)
        started = [
            {
                "key": phase["key"],
                "label": phase["label"],
                "seconds": float(phase.get("seconds", 0.0)),
                "status": "pending",
            }
            for phase in phases
        ]
        return tx.insert_task(
            kind,
            phases=started,
            input_data=input_data or {},
            parent_id=parent_id,
            result=result,
        )

    # -- advancing ----------------------------------------------------
    def tick(self) -> bool:
        candidate = self.service.store.next_due_task(self.service.clock.monotonic())
        if candidate is None:
            return False
        scope, task = candidate
        workspace = self.service.workspace(scope.scenario_id)
        if workspace["workspace_epoch"] != scope.workspace_epoch:
            return False
        return self.service.store.advance_task(
            scope,
            task["id"],
            task["phase_index"],
            self._publisher(scope, workspace, task),
        )

    def advance_in_transaction(self, tx, task_id: str) -> bool:
        task = tx.task_row(task_id)
        if task["status"] not in RUNNING_STATUSES:
            raise DemoError("task_not_advanceable", "该任务不能手动推进。", status=409)
        workspace = tx.workspace()
        publisher = self._publisher(
            Scope(tx.scope.scenario_id, tx.scope.workspace_epoch), workspace, task
        )
        return tx.advance_task(task_id, task["phase_index"], publisher)

    def _publisher(self, scope: Scope, workspace: JsonObject, task: JsonObject):
        phases, publish = self.definition(task["kind"])

        def run(tx, task_row: JsonObject) -> JsonObject:
            index = task_row["phase_index"]
            stored = task_row.get("phases") or phases
            phase = stored[index] if index < len(stored) else {"key": "done", "label": "完成"}
            deadline = (task_row.get("input") or {}).get("card_not_before")
            if deadline and phase["key"] == "organize":
                remaining = (datetime.fromisoformat(deadline) - datetime.fromisoformat(self.service.clock.now_iso())).total_seconds()
                if remaining > 0:
                    return {"defer_s": remaining}
            feedback_deadline = (task_row.get("input") or {}).get("ready_not_before")
            if task_row["kind"] == "feedback.review" and feedback_deadline and phase["key"] == "publish":
                remaining = (datetime.fromisoformat(feedback_deadline) - datetime.fromisoformat(self.service.clock.now_iso())).total_seconds()
                if remaining > 0:
                    return {"defer_s": remaining}
            if task_row["kind"] in ("feedback.review", "knowledge.collect"):
                try:
                    with tx.savepoint():
                        update = publish(tx, task_row, phase) or {}
                except Exception:
                    if task_row["kind"] == "feedback.review":
                        from .feedback import sync_analysis_status
                        sync_analysis_status(tx, task_row, "failed")
                        result = {"analysis_id": task_row["input"]["analysis_id"],
                                  "action": "failed", "reason": "重新分析未完成，请重试。", "change_set_id": None}
                    else:
                        result = {"reason": "资料整理未完成，请重试。"}
                    update = {"status": "failed", "result": result}
            else:
                update = publish(tx, task_row, phase) or {}
            if "delay_s" not in update:
                if workspace["mode"] == "fast":
                    update["delay_s"] = 0.15
                elif workspace["mode"] == "step":
                    update["delay_s"] = 0.0
                else:
                    update["delay_s"] = float(phase.get("seconds", 0.0))
            return update

        return run

    # -- worker lifecycle --------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="demo-tasks", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(0.05):
            try:
                self.tick()
            except Exception:
                # A failing demo task must not kill the presentation worker.
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
