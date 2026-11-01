"""Stable error envelope, codes and recovery actions.

Transcribed from ``shared/error-conventions.md`` §2—§3.  The table below is the
only place a code is bound to its category, HTTP status and retry policy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from game_app.contracts.primitives import ID, ObjectRef, Revision, StrictModel


class ErrorCategory(StrEnum):
    validation = "validation"
    conflict = "conflict"
    not_found = "not_found"
    security = "security"
    dependency = "dependency"
    external = "external"
    storage = "storage"
    internal = "internal"


class ErrorCode(StrEnum):
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    INPUT_VALIDATION_FAILED = "INPUT_VALIDATION_FAILED"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    SESSION_CONTEXT_INVALID = "SESSION_CONTEXT_INVALID"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    RESOURCE_GONE = "RESOURCE_GONE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CURRENT_CONFIGURATION_CONFLICT = "CURRENT_CONFIGURATION_CONFLICT"
    INSTALLATION_REVISION_CONFLICT = "INSTALLATION_REVISION_CONFLICT"
    LOCAL_SETTING_CONFLICT = "LOCAL_SETTING_CONFLICT"
    PREFERENCE_VERSION_CONFLICT = "PREFERENCE_VERSION_CONFLICT"
    TARGET_ACCEPTANCE_CONFLICT = "TARGET_ACCEPTANCE_CONFLICT"
    BINDING_REVISION_CONFLICT = "BINDING_REVISION_CONFLICT"
    INFORMATION_REQUEST_VERSION_CONFLICT = "INFORMATION_REQUEST_VERSION_CONFLICT"
    INFORMATION_REQUEST_STATE_CONFLICT = "INFORMATION_REQUEST_STATE_CONFLICT"
    INFORMATION_CONTEXT_CHANGED = "INFORMATION_CONTEXT_CHANGED"
    TASK_STATE_CONFLICT = "TASK_STATE_CONFLICT"
    ACTIVE_CAPTURE_EXISTS = "ACTIVE_CAPTURE_EXISTS"
    REFERENCE_INVALID = "REFERENCE_INVALID"
    BASELINE_REQUIRED = "BASELINE_REQUIRED"
    PRECONDITION_UNVERIFIED = "PRECONDITION_UNVERIFIED"
    UNSUPPORTED_ARTIFACT_FORMAT = "UNSUPPORTED_ARTIFACT_FORMAT"
    IMPORT_SNAPSHOT_INCONSISTENT = "IMPORT_SNAPSHOT_INCONSISTENT"
    ARTIFACT_UNAVAILABLE = "ARTIFACT_UNAVAILABLE"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    CALL_OUTCOME_UNKNOWN = "CALL_OUTCOME_UNKNOWN"
    STORAGE_COMMIT_FAILED = "STORAGE_COMMIT_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class RetryPolicy(StrEnum):
    none = "none"
    same_submission = "same_submission"
    refresh_then_new_submission = "refresh_then_new_submission"
    repair_then_new_submission = "repair_then_new_submission"
    resume_as_new_task = "resume_as_new_task"
    verify_external_outcome = "verify_external_outcome"


class RecoveryActionKind(StrEnum):
    edit_input = "edit_input"
    read_task = "read_task"
    read_information_request = "read_information_request"
    review_configuration = "review_configuration"
    view_saved_results = "view_saved_results"
    reupload_file = "reupload_file"
    configure_provider = "configure_provider"
    resume_task = "resume_task"
    create_new_task = "create_new_task"
    verify_external_call = "verify_external_call"
    restart_local_application = "restart_local_application"


# code -> (category, http status, retry policy, default recovery action)
_CODE_META: dict[ErrorCode, tuple[ErrorCategory, int, RetryPolicy, RecoveryActionKind]] = {
    ErrorCode.MALFORMED_REQUEST: (ErrorCategory.validation, 400, RetryPolicy.none, RecoveryActionKind.edit_input),
    ErrorCode.INPUT_VALIDATION_FAILED: (ErrorCategory.validation, 422, RetryPolicy.none, RecoveryActionKind.edit_input),
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED: (ErrorCategory.validation, 400, RetryPolicy.none, RecoveryActionKind.edit_input),
    ErrorCode.SESSION_CONTEXT_INVALID: (ErrorCategory.security, 403, RetryPolicy.same_submission, RecoveryActionKind.restart_local_application),
    ErrorCode.RESOURCE_NOT_FOUND: (ErrorCategory.not_found, 404, RetryPolicy.none, RecoveryActionKind.edit_input),
    ErrorCode.RESOURCE_GONE: (ErrorCategory.not_found, 410, RetryPolicy.none, RecoveryActionKind.view_saved_results),
    ErrorCode.IDEMPOTENCY_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.edit_input),
    ErrorCode.CURRENT_CONFIGURATION_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.review_configuration),
    ErrorCode.INSTALLATION_REVISION_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.view_saved_results),
    ErrorCode.LOCAL_SETTING_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.view_saved_results),
    ErrorCode.PREFERENCE_VERSION_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.view_saved_results),
    ErrorCode.TARGET_ACCEPTANCE_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.view_saved_results),
    ErrorCode.BINDING_REVISION_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.view_saved_results),
    ErrorCode.INFORMATION_REQUEST_VERSION_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.read_information_request),
    ErrorCode.INFORMATION_REQUEST_STATE_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.read_information_request),
    ErrorCode.INFORMATION_CONTEXT_CHANGED: (ErrorCategory.conflict, 409, RetryPolicy.refresh_then_new_submission, RecoveryActionKind.read_information_request),
    ErrorCode.TASK_STATE_CONFLICT: (ErrorCategory.conflict, 409, RetryPolicy.none, RecoveryActionKind.read_task),
    ErrorCode.ACTIVE_CAPTURE_EXISTS: (ErrorCategory.conflict, 409, RetryPolicy.none, RecoveryActionKind.read_task),
    ErrorCode.REFERENCE_INVALID: (ErrorCategory.dependency, 422, RetryPolicy.none, RecoveryActionKind.edit_input),
    ErrorCode.BASELINE_REQUIRED: (ErrorCategory.dependency, 409, RetryPolicy.repair_then_new_submission, RecoveryActionKind.review_configuration),
    ErrorCode.PRECONDITION_UNVERIFIED: (ErrorCategory.dependency, 409, RetryPolicy.repair_then_new_submission, RecoveryActionKind.view_saved_results),
    ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT: (ErrorCategory.validation, 422, RetryPolicy.repair_then_new_submission, RecoveryActionKind.reupload_file),
    ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT: (ErrorCategory.storage, 409, RetryPolicy.repair_then_new_submission, RecoveryActionKind.view_saved_results),
    ErrorCode.ARTIFACT_UNAVAILABLE: (ErrorCategory.storage, 409, RetryPolicy.repair_then_new_submission, RecoveryActionKind.reupload_file),
    ErrorCode.PROVIDER_NOT_CONFIGURED: (ErrorCategory.external, 503, RetryPolicy.repair_then_new_submission, RecoveryActionKind.configure_provider),
    ErrorCode.PROVIDER_AUTH_FAILED: (ErrorCategory.external, 503, RetryPolicy.repair_then_new_submission, RecoveryActionKind.configure_provider),
    ErrorCode.PROVIDER_RATE_LIMITED: (ErrorCategory.external, 503, RetryPolicy.resume_as_new_task, RecoveryActionKind.resume_task),
    ErrorCode.PROVIDER_FAILED: (ErrorCategory.external, 502, RetryPolicy.resume_as_new_task, RecoveryActionKind.resume_task),
    ErrorCode.CALL_OUTCOME_UNKNOWN: (ErrorCategory.external, 409, RetryPolicy.verify_external_outcome, RecoveryActionKind.verify_external_call),
    ErrorCode.STORAGE_COMMIT_FAILED: (ErrorCategory.storage, 503, RetryPolicy.same_submission, RecoveryActionKind.read_task),
    ErrorCode.INTERNAL_ERROR: (ErrorCategory.internal, 500, RetryPolicy.same_submission, RecoveryActionKind.read_task),
}


class FieldError(StrictModel):
    path: str
    code: str
    message: str

    @field_validator("code")
    @classmethod
    def known_code(cls, value: str) -> str:
        allowed = {"required", "type", "enum", "range", "reference", "conflict", "unsupported_field"}
        if value not in allowed:
            raise ValueError(f"unknown field error code: {value}")
        return value


class ErrorDetails(StrictModel):
    expected_revision: Revision | None
    current_revision: Revision | None
    current_resource_ref: ObjectRef | None
    replacement_resource_ref: ObjectRef | None
    call_id: ID | None
    saved_result_refs: list[ObjectRef]


class RecoveryAction(StrictModel):
    kind: RecoveryActionKind
    target_ref: ObjectRef | None
    label: str


class ErrorEnvelope(StrictModel):
    code: ErrorCode
    category: ErrorCategory
    message: str
    request_id: ID
    field_errors: list[FieldError] = Field(default_factory=list)
    resource_refs: list[ObjectRef] = Field(default_factory=list)
    retry_policy: RetryPolicy
    actions: list[RecoveryAction]
    retry_after_seconds: int | None = None
    details: ErrorDetails


def empty_details(
    *,
    expected_revision: int | None = None,
    current_revision: int | None = None,
    current_resource_ref: ObjectRef | None = None,
    replacement_resource_ref: ObjectRef | None = None,
    call_id: str | None = None,
    saved_result_refs: list[ObjectRef] | None = None,
) -> ErrorDetails:
    return ErrorDetails(
        expected_revision=expected_revision,
        current_revision=current_revision,
        current_resource_ref=current_resource_ref,
        replacement_resource_ref=replacement_resource_ref,
        call_id=call_id,
        saved_result_refs=saved_result_refs or [],
    )


class ContractError(Exception):
    """A contract failure with a stable code; never a stack trace to the user."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        field_errors: list[FieldError] | None = None,
        resource_refs: list[ObjectRef] | None = None,
        retry_policy: RetryPolicy | None = None,
        actions: list[RecoveryAction] | None = None,
        retry_after_seconds: int | None = None,
        details: ErrorDetails | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        category, http_status, default_retry, default_action = _CODE_META[code]
        self.category = category
        self.http_status = http_status
        self.retry_policy = retry_policy or default_retry
        self.field_errors = list(field_errors or [])
        self.resource_refs = list(resource_refs or [])
        self.actions = list(actions or [RecoveryAction(
            kind=default_action, target_ref=None, label=default_action.value
        )])
        self.retry_after_seconds = retry_after_seconds
        self.details = details or empty_details()

    def to_envelope(self, request_id: str) -> ErrorEnvelope:
        return ErrorEnvelope(
            code=self.code,
            category=self.category,
            message=self.message,
            request_id=request_id,
            field_errors=self.field_errors,
            resource_refs=self.resource_refs,
            retry_policy=self.retry_policy,
            actions=self.actions,
            retry_after_seconds=self.retry_after_seconds,
            details=self.details,
        )

    def to_dict(self, request_id: str) -> dict[str, Any]:
        return self.to_envelope(request_id).model_dump(mode="json")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ContractError({self.code.value}, {self.message!r})"


__all__ = [
    "ErrorCategory",
    "ErrorCode",
    "RetryPolicy",
    "RecoveryActionKind",
    "FieldError",
    "ErrorDetails",
    "RecoveryAction",
    "ErrorEnvelope",
    "ContractError",
    "empty_details",
]
