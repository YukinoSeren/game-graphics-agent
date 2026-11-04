"""Shared enums and the entity-type registry.

The value sets are transcribed from ``shared/state-conventions.md`` §2, the
single table every module must reference.  No module defines a second copy.
"""

from __future__ import annotations

from enum import StrEnum


class ValueState(StrEnum):
    known = "known"
    unknown = "unknown"
    conflicting = "conflicting"
    not_applicable = "not_applicable"


class GameIdentityStatus(StrEnum):
    provisional = "provisional"
    verified = "verified"
    legacy_unverified = "legacy_unverified"


class AcquisitionStatus(StrEnum):
    not_attempted = "not_attempted"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class AcquisitionMethod(StrEnum):
    system_probe = "system_probe"
    user_provided = "user_provided"
    player_confirmed = "player_confirmed"
    source_statement = "source_statement"
    source_author_measurement = "source_author_measurement"
    agent_derived = "agent_derived"


class CheckStatus(StrEnum):
    not_run = "not_run"
    pass_ = "pass"
    fail = "fail"
    unknown = "unknown"
    not_applicable = "not_applicable"


class LegacyRecordState(StrEnum):
    accepted = "accepted"
    quarantined = "quarantined"


class CollectionResult(StrEnum):
    completed_scope = "completed_scope"
    partial_scope = "partial_scope"


class CollectionStopReason(StrEnum):
    coverage_reached = "coverage_reached"
    low_information_gain = "low_information_gain"
    budget_exhausted = "budget_exhausted"
    access_blocked = "access_blocked"
    source_unavailable = "source_unavailable"
    unresolved_conflict = "unresolved_conflict"


class PublicationStatus(StrEnum):
    staging = "staging"
    validated = "validated"
    published = "published"
    rejected = "rejected"
    tombstoned = "tombstoned"


class CoverageStatus(StrEnum):
    covered = "covered"
    partially_covered = "partially_covered"
    unknown = "unknown"
    not_applicable = "not_applicable"


class GapPriority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class GapStatus(StrEnum):
    open = "open"
    resolved = "resolved"
    stopped = "stopped"


class RecommendationReadiness(StrEnum):
    actionable = "actionable"
    provisional = "provisional"
    blocked = "blocked"


class RecommendationActionKind(StrEnum):
    set_value = "set_value"
    keep_current = "keep_current"
    conditional_strategy = "conditional_strategy"
    check_in_game = "check_in_game"
    defer = "defer"


class ReviewState(StrEnum):
    current = "current"
    needs_review = "needs_review"
    superseded = "superseded"


class AppliedOrigin(StrEnum):
    baseline = "baseline"
    recommendation = "recommendation"
    change_set = "change_set"
    manual_edit = "manual_edit"
    rollback = "rollback"


class ConfigurationRegistrationKind(StrEnum):
    current_adoption = "current_adoption"
    historical_record = "historical_record"


class ActivationState(StrEnum):
    active_confirmed = "active_confirmed"
    restart_pending = "restart_pending"
    unknown = "unknown"
    conflicting = "conflicting"
    not_applicable = "not_applicable"


class ConfigurationState(StrEnum):
    bound = "bound"
    unverified = "unverified"
    mixed = "mixed"


class SessionLifecycle(StrEnum):
    active = "active"
    ended = "ended"


class FrameStreamKind(StrEnum):
    application_present = "application_present"
    displayed = "displayed"
    unknown = "unknown"


class LogQuality(StrEnum):
    valid = "valid"
    limited = "limited"
    invalid = "invalid"


class ThresholdMode(StrEnum):
    fixed = "fixed"
    baseline_derived = "baseline_derived"


class ComparabilityLevel(StrEnum):
    controlled = "controlled"
    approximate = "approximate"
    descriptive = "descriptive"


class InterpretationStatus(StrEnum):
    unparsed = "unparsed"
    explicit = "explicit"
    inferred = "inferred"
    uncertain = "uncertain"


class PreferenceScope(StrEnum):
    experiment = "experiment"
    game = "game"
    global_ = "global"


class PreferenceExplicitness(StrEnum):
    explicit = "explicit"
    tentative = "tentative"


class PreferenceLifecycle(StrEnum):
    active = "active"
    withdrawn = "withdrawn"


class NextActionKind(StrEnum):
    keep = "keep"
    adjust = "adjust"
    measure = "measure"
    research = "research"
    insufficient_evidence = "insufficient_evidence"


class LegacyOutcome(StrEnum):
    accepted = "accepted"
    improved_but_rejected = "improved_but_rejected"
    no_clear_change = "no_clear_change"
    worse = "worse"
    inconclusive = "inconclusive"


class PlayerAcceptance(StrEnum):
    accepted = "accepted"
    rejected = "rejected"
    unknown = "unknown"


class EffectAssessment(StrEnum):
    improved = "improved"
    no_clear_change = "no_clear_change"
    worse = "worse"
    inconclusive = "inconclusive"
    not_measured = "not_measured"


class TaskType(StrEnum):
    device_scan = "device_scan"
    knowledge = "knowledge"
    knowledge_import = "knowledge_import"
    recommendation = "recommendation"
    analysis = "analysis"
    capture = "capture"
    log_import = "log_import"
    feedback = "feedback"
    next_action = "next_action"


class TaskState(StrEnum):
    queued = "queued"
    running = "running"
    waiting_input = "waiting_input"
    completed = "completed"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"


TERMINAL_TASK_STATES = frozenset({
    TaskState.completed,
    TaskState.partial,
    TaskState.failed,
    TaskState.cancelled,
    TaskState.interrupted,
})


class InformationRequestState(StrEnum):
    open = "open"
    partially_answered = "partially_answered"
    resolved = "resolved"
    skipped = "skipped"
    superseded = "superseded"


class InformationContinuationMode(StrEnum):
    source_task = "source_task"
    facts_only = "facts_only"


class AnswerState(StrEnum):
    unanswered = "unanswered"
    unresolved = "unresolved"
    answered = "answered"
    skipped = "skipped"


class ExternalCallState(StrEnum):
    prepared = "prepared"
    in_flight = "in_flight"
    succeeded = "succeeded"
    failed_known = "failed_known"
    outcome_unknown = "outcome_unknown"


class BillingState(StrEnum):
    not_applicable = "not_applicable"
    not_reported = "not_reported"
    reported = "reported"
    unknown = "unknown"


class IdempotencyLifecycle(StrEnum):
    registered = "registered"
    tombstoned = "tombstoned"


class StateDimension(StrEnum):
    publication = "publication"
    call = "call"
    request = "request"
    session = "session"


class FileOperationKind(StrEnum):
    upload = "upload"
    import_ = "import"
    export = "export"


class FileOperationPhase(StrEnum):
    prepared = "prepared"
    files_ready = "files_ready"
    committed = "committed"
    abandoned = "abandoned"


class ExecutionStopCode(StrEnum):
    user_cancelled = "user_cancelled"
    process_exit = "process_exit"
    provider_error = "provider_error"
    validation_failed = "validation_failed"
    storage_error = "storage_error"
    input_unresolved = "input_unresolved"
    unsupported_format = "unsupported_format"
    scope_completed = "scope_completed"


# Entity types registered for ObjectRef/VersionRef.  Adding a type is a contract
# change owned by 07; arbitrary strings must never be accepted.
ENTITY_TYPES = frozenset({
    "Game",
    "GameAlias",
    "GameSelection",
    "IdentityMapping",
    "GameInstallation",
    "InstallationRegistration",
    "LocalPathBinding",
    "LocalSettingDefinition",
    "LocalSettingRegistration",
    "GameContext",
    "DeviceSnapshot",
    "Source",
    "SourceRevision",
    "SourceRelation",
    "ImportSnapshot",
    "ImportedRecord",
    "ImportedExtraction",
    "KnowledgePackage",
    "ImpactNotice",
    "MetricDefinition",
    "RecommendationVersion",
    "TargetProposal",
    "TargetAcceptance",
    "RecommendationReviewEvent",
    "AppliedConfiguration",
    "PlayerDeclaration",
    "ConfigurationObservation",
    "ConfigurationBinding",
    "CurrentAppliedEvent",
    "PlaySession",
    "SessionSegment",
    "CaptureArtifact",
    "CaptureLease",
    "TestProtocol",
    "ComparisonProtocol",
    "PerformanceSummary",
    "PerformanceComparison",
    "Feedback",
    "FeedbackInterpretationLink",
    "Preference",
    "PreferenceDeclaration",
    "Diagnosis",
    "ChangeSet",
    "ExperimentOutcome",
    "TaskRun",
    "TaskInputRevision",
    "TaskEvent",
    "TaskCheckpoint",
    "InformationRequest",
    "InformationAmendment",
    "InformationResponse",
    "ExternalCallRecord",
    "IdempotencyRecord",
    "ManagedFile",
    "StateEvent",
})
