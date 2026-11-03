"""The packaged metric rule registry ``project-metrics/1`` (04 §4.4).

The four rules below are the project's own algorithms from 04 §6.  They are
versioned by content: a rule's hash enters every protocol declaration, so a
changed algorithm can never be silently substituted under the same key.  No
rule claims a real capture tool or a frame stream it was not verified with.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from game_app.contracts.enums import FrameStreamKind
from game_app.contracts.performance import MetricChoice, MetricChoicePage
from game_app.repository import canonical_json

REGISTRY_VERSION = "project-metrics/1"


@dataclass(frozen=True)
class MetricRule:
    metric_key: str
    definition_version: str
    label: str
    unit: str
    frame_stream_kind: FrameStreamKind
    algorithm_ref: str
    minimum_sample_rule: str

    @property
    def key(self) -> str:
        return f"{self.metric_key}/{self.definition_version}"

    def manifest(self) -> dict:
        return {
            "registry_version": REGISTRY_VERSION,
            "metric_key": self.metric_key,
            "definition_version": self.definition_version,
            "label": self.label,
            "unit": self.unit,
            "frame_stream_kind": self.frame_stream_kind.value,
            "algorithm_ref": self.algorithm_ref,
            "minimum_sample_rule": self.minimum_sample_rule,
        }

    @property
    def rule_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.manifest())).hexdigest()


RULES: tuple[MetricRule, ...] = (
    MetricRule(
        metric_key="average_present_rate",
        definition_version="v1",
        label="Average present rate",
        unit="fps",
        frame_stream_kind=FrameStreamKind.application_present,
        algorithm_ref="04 §6 step 3: F = 1000*m / sum(d) over retained intervals",
        minimum_sample_rule="m >= 1 and sum(d) > 0; otherwise the value stays unknown",
    ),
    MetricRule(
        metric_key="interval_p95_nearest_rank",
        definition_version="v1",
        label="Interval P95 (nearest rank)",
        unit="ms",
        frame_stream_kind=FrameStreamKind.application_present,
        algorithm_ref="04 §6 step 4: ascending item ceil(0.95*m)",
        minimum_sample_rule=(
            "m >= 1; the project's separate tail-sample threshold decides whether it is"
            " displayed"
        ),
    ),
    MetricRule(
        metric_key="interval_p99_nearest_rank",
        definition_version="v1",
        label="Interval P99 (nearest rank)",
        unit="ms",
        frame_stream_kind=FrameStreamKind.application_present,
        algorithm_ref="04 §6 step 4: ascending item ceil(0.99*m)",
        minimum_sample_rule=(
            "m >= 1; a nearest-rank percentile needs no tail-sample gate"
        ),
    ),
    MetricRule(
        metric_key="slowest_1pct_inverse_mean",
        definition_version="v1",
        label="Slowest 1% inverse mean",
        unit="fps",
        frame_stream_kind=FrameStreamKind.application_present,
        algorithm_ref=(
            "04 §6 step 4: reciprocal of the mean of the largest ceil(0.01*m) intervals"
        ),
        minimum_sample_rule=(
            "ceil(0.01*m) >= 20; otherwise the value stays unknown"
        ),
    ),
)


def registry_manifest() -> dict:
    return {
        "registry_version": REGISTRY_VERSION,
        "rules": [
            {**rule.manifest(), "rule_hash": rule.rule_hash} for rule in RULES
        ],
    }


def registry_manifest_hash() -> str:
    return hashlib.sha256(canonical_json(registry_manifest())).hexdigest()


def rule_for(key: str) -> MetricRule | None:
    return next((rule for rule in RULES if rule.key == key), None)


def catalog(
    frame_stream_kind: FrameStreamKind | None,
    definition_ids: Mapping[str, str],
) -> MetricChoicePage:
    """Read-only view over the packaged rules and the registered definitions.

    An empty database has no definition ids; the page still names every rule
    so a client can choose one and let the backend allocate the definition.
    """
    choices = [
        MetricChoice(
            key=rule.key,
            label=rule.label,
            unit=rule.unit,
            frame_stream_kind=rule.frame_stream_kind,
            definition_id=definition_ids.get(rule.key),
        )
        for rule in RULES
        if frame_stream_kind is None or rule.frame_stream_kind is frame_stream_kind
    ]
    return MetricChoicePage(choices=choices, registry_version=REGISTRY_VERSION)


__all__ = [
    "REGISTRY_VERSION",
    "RULES",
    "MetricRule",
    "registry_manifest",
    "registry_manifest_hash",
    "rule_for",
    "catalog",
]
