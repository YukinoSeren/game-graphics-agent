"""Explicit action registry for the temporary demo.

Only implemented actions are registered. Unknown actions fail; they never
fall through to a success reply.
"""

from __future__ import annotations

from .service import DemoService

# Fixed ownership map from the implementation plan. Actions are registered by
# the task that implements them; this map documents the final set.
ACTION_OWNERS = {
    "context.save": "T06",
    "device.scan": "T06",
    "device.check": "local-device-profile",
    "intent.submit": "conversation-onboarding",
    "information.answer": "T06",
    "information.amend": "T06",
    "knowledge.collect": "T07",
    "knowledge.import": "T07",
    "knowledge.followup": "T07",
    "knowledge.update": "T07",
    "recommendation.generate": "T08",
    "recommendation.variant": "T08",
    "recommendation.review": "T08",
    "setting.register": "T09",
    "configuration.adopt": "T09",
    "configuration.edit": "T09",
    "configuration.rollback": "T09",
    "configuration.record_history": "T09",
    "plan.save": "T10",
    "session.create": "T10",
    "session.split": "T10",
    "session.end": "T10",
    "binding.revise": "T10",
    "capture.start": "T12",
    "capture.stop": "T12",
    "log.inspect": "T12",
    "log.associate": "T12",
    "log.capture_recent": "recent-virtual-play-logs",
    "performance.analyze": "T12",
    "comparison.freeze": "T13",
    "comparison.run": "T13",
    "feedback.save": "T14",
    "feedback.next": "T14",
    "feedback.review": "flow-feedback",
    "preference.save": "T14",
    "target.respond": "T14",
    "outcome.save": "T14",
    "task.cancel": "T05",
    "task.resume": "T05",
    "task.advance": "T05",
    "demo.mode": "T05",
    "demo.script": "T05",
    "demo.reset": "T05",
    "demo.seed": "T15",
    "export.create": "T15",
    "backup.create": "T15",
    "backup.restore": "T15",
}


def register_builtin_actions(service: DemoService) -> None:
    """Register the actions implemented so far.

    Each owning task adds its own group here. T03's command channel and the
    T05 task controls are live before any domain content action exists.
    """
    from . import (
        comparison,
        configuration,
        controls,
        device,
        exports,
        feedback,
        intents,
        knowledge,
        performance,
        play_logs,
        recommendation,
        seeding,
        sessions,
    )

    controls.register(service)
    device.register(service)
    knowledge.register(service)
    intents.register(service)
    recommendation.register(service)
    configuration.register(service)
    sessions.register(service)
    performance.register(service)
    play_logs.register(service)
    comparison.register(service)
    feedback.register(service)
    seeding.register(service)
    exports.register(service)
