"""Logical contract v0.1. Unknowns and source scopes must survive serialization."""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

Scalar = StrictStr | StrictInt | StrictFloat | StrictBool
Reason = Literal["not_provided", "scope_ambiguous", "parse_failed", "conflicting", "unresolved_choice"]
Scope = Literal["page", "section", "table", "row", "cell", "run"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Proof(Model):
    block_id: str
    quote: str = Field(min_length=1, max_length=600)
    scope: Scope
    row: int | None = Field(default=None, ge=0)
    column: int | None = Field(default=None, ge=0)


class Branch(Model):
    condition: str = Field(min_length=1)
    value: Scalar


class SettingValue(Model):
    value_kind: Literal["fixed", "alternatives", "conditional", "unknown"]
    value: Scalar | None = None
    alternatives: list[Scalar] = Field(default_factory=list)
    branches: list[Branch] = Field(default_factory=list)
    unknown_reason: Reason | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def check_shape(self):
        shapes = {
            "fixed": self.value is not None and not self.alternatives and not self.branches and self.unknown_reason is None,
            "alternatives": self.value is None and bool(self.alternatives) and not self.branches and self.unknown_reason is None,
            "conditional": self.value is None and bool(self.branches) and not self.alternatives and self.unknown_reason is None,
            "unknown": self.value is None and self.unknown_reason is not None and not self.alternatives and not self.branches,
        }
        if not shapes[self.value_kind]:
            raise ValueError("value_kind and payload disagree")
        return self


class NumericValue(Model):
    number_kind: Literal["exact", "approximate", "interval", "lower_bound", "upper_bound", "unknown"]
    value: StrictInt | StrictFloat | None = None
    lower: StrictInt | StrictFloat | None = None
    upper: StrictInt | StrictFloat | None = None
    unknown_reason: Reason | None = None

    @model_validator(mode="after")
    def check_shape(self):
        if self.number_kind in {"exact", "approximate"}:
            valid = self.value is not None and self.lower is None and self.upper is None and self.unknown_reason is None
        elif self.number_kind == "interval":
            valid = self.value is None and self.lower is not None and self.upper is not None and self.lower <= self.upper and self.unknown_reason is None
        elif self.number_kind == "lower_bound":
            valid = self.value is None and self.lower is not None and self.upper is None and self.unknown_reason is None
        elif self.number_kind == "upper_bound":
            valid = self.value is None and self.upper is not None and self.lower is None and self.unknown_reason is None
        else:
            valid = self.value is None and self.lower is None and self.upper is None and self.unknown_reason is not None
        if not valid:
            raise ValueError("invalid numeric shape")
        return self


class EnvironmentField(Model):
    value: Scalar | None
    unknown_reason: Reason | None = None
    proof: Proof | None = None

    @model_validator(mode="after")
    def check_unknown(self):
        if self.value is None and self.unknown_reason is None:
            raise ValueError("null environment field requires unknown_reason")
        if self.value is not None and self.proof is None:
            raise ValueError("known environment field requires its own source scope")
        return self


class Setting(Model):
    id: str
    raw_name: str
    canonical_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    proof: Proof


class Assignment(Model):
    setting_id: str
    value: SettingValue
    evidence_ids: list[str] = Field(min_length=1)


class Configuration(Model):
    id: str
    name: str
    kind: Literal["preset_reference", "conditional_template", "resolved_configuration", "run_configuration"]
    assignments: list[Assignment] = Field(default_factory=list)
    preset_reference: str | None = None
    environment_ids: list[str] = Field(default_factory=list)
    source_capture_complete: bool = False
    runtime_complete: bool = False
    proof: Proof

    @model_validator(mode="after")
    def check_configuration(self):
        if not self.assignments and not self.preset_reference:
            raise ValueError("configuration needs assignments or an explicit preset reference")
        if self.kind == "resolved_configuration" and any(a.value.value_kind != "fixed" for a in self.assignments):
            raise ValueError("resolved_configuration cannot contain unresolved assignments")
        return self


class Environment(Model):
    id: str
    fields: dict[str, EnvironmentField]


class Evidence(Model):
    id: str
    kind: Literal["setting_description", "constraint", "recommendation", "measurement", "observation", "version_change", "inference"]
    origin: Literal["source_statement", "source_author_measurement", "agent_derived"]
    statement: str
    proof: Proof | None = None
    input_ids: list[str] = Field(default_factory=list)
    setting_ids: list[str] = Field(default_factory=list)
    configuration_id: str | None = None
    environment_id: str | None = None
    metric: Literal["average_fps", "fps_unspecified", "one_percent_low_fps", "point_one_percent_low_fps", "frame_time", "memory_usage", "latency"] | None = None
    numeric: NumericValue | None = None
    unit: str | None = None
    detected_state: Literal["detected", "not_detected", "unknown"] | None = None
    conversion: str | None = None
    conflicts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_origin(self):
        if self.origin == "agent_derived" and not self.input_ids:
            raise ValueError("agent derivation requires input evidence")
        if self.origin != "agent_derived" and self.proof is None:
            raise ValueError("source claim requires proof")
        if self.numeric is not None and (self.metric is None or self.unit is None):
            raise ValueError("numeric evidence requires metric and unit")
        if self.kind == "measurement" and self.numeric is None:
            raise ValueError("measurement requires numeric value")
        return self


class Comparison(Model):
    id: str
    measurement_ids: list[str] = Field(min_length=2, max_length=2)
    strictly_comparable: bool = False
    rationale: str
    proof: Proof | None = None
    formula: str | None = None


class Constraint(Model):
    id: str
    evidence_id: str
    kind: Literal["support", "exclusion", "dependency", "suspected_conflict"]
    # Only declarative equality predicates are executable. Other rules remain questions.
    when: dict[str, Scalar] = Field(default_factory=dict)
    setting_id: str
    affected_values: list[Scalar] = Field(default_factory=list)
    requires: dict[str, Scalar] = Field(default_factory=dict)
    verified_scope: bool = False
    scope_note: str


class VersionImpact(Model):
    id: str
    evidence_id: str
    setting_id: str
    affected_value: Scalar | None = None
    platform: str
    version_raw: str | None = None
    effective_date: str | None = None
    review_record_ids: list[str] = Field(default_factory=list)


class Gap(Model):
    id: str
    record_ids: list[str] = Field(default_factory=list)
    field: str
    reason: str
    question: str
    priority: Literal["P0", "P1", "P2"]
    query: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    status: Literal["open", "resolved", "stopped"] = "open"
    resolution_evidence_ids: list[str] = Field(default_factory=list)
    attempts: list[dict] = Field(default_factory=list)
    stop_reason: str | None = None


class Resolution(Model):
    gap_id: str
    field: str
    answer: Scalar
    scope: Scope
    evidence_ids: list[str] = Field(min_length=1)
    explanation: str


class SourceMetadata(Model):
    author: str | None = None
    published_date: str | None = None
    updated_date: str | None = None
    test_date: str | None = None
    original_source_url: str | None = None


class Extraction(Model):
    game_match: Literal["matched", "uncertain", "mismatch"]
    game_proof: Proof
    platform: str | None = None
    metadata: SourceMetadata = Field(default_factory=SourceMetadata)
    settings: list[Setting] = Field(default_factory=list)
    configurations: list[Configuration] = Field(default_factory=list)
    environments: list[Environment] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    comparisons: list[Comparison] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    version_impacts: list[VersionImpact] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    resolutions: list[Resolution] = Field(default_factory=list)


class Block(Model):
    id: str
    kind: Literal["paragraph", "table", "heading"]
    section: list[str] = Field(default_factory=list)
    text: str
    rows: list[list[str]] = Field(default_factory=list)


class Document(Model):
    url: str
    title: str
    retrieved_at: str = Field(default_factory=now)
    revision_id: str = Field(default_factory=lambda: uuid4().hex)
    content_hash: str
    blocks: list[Block]
    images: list[dict] = Field(default_factory=list)
    retrieval_method: str = "html"
    capture_scope: str = "selected_article_blocks"
    truncated: bool = False


RECORD_GROUPS = ("settings", "configurations", "environments", "evidence", "comparisons", "constraints", "version_impacts")
