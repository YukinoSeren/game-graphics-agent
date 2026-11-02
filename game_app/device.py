"""Read-only device acquisition, source wrapping and failure questions (01).

The service turns probe readings into a versioned ``DeviceSnapshot`` where every
known value cites a published original.  A failed probe never becomes a default
value: it becomes an unknown field plus a question, and an empty GPU list is
always explained by a failed check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from game_app.contracts.common import (
    AcquisitionAttempt,
    CheckResult,
    EvidenceRef,
    FieldValue,
    Provenance,
)
from game_app.contracts.device import (
    DeviceSnapshot,
    DisplayContext,
    GpuSnapshot,
    Resolution,
)
from game_app.contracts.enums import (
    AcquisitionMethod,
    AcquisitionStatus,
    AnswerState,
    CheckStatus,
    InformationContinuationMode,
    InformationRequestState,
    TaskState,
    TaskType,
    ValueState,
)
from game_app.contracts.information import InformationRequest, RequestedField
from game_app.contracts.primitives import ObjectRef, VersionRef, new_id, utc_now
from game_app.contracts.tasks import DeviceScanInput, DomainResult
from game_app.errors import ContractError, ErrorCode, FieldError
from game_app.files import FileService
from game_app.repository import ProductRepository
from game_app.tasks import HandlerOutcome, TaskContext

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
DEFAULT_PROBE_TIMEOUT_SECONDS = 10.0

_GPU_PATH = re.compile(r"^gpus\.(\d+)\.(raw_name|vram_bytes|driver_version)$")

_SCALAR_PATHS = frozenset({
    "cpu",
    "memory_bytes",
    "os_version",
    "device_form",
    "display.desktop_resolution.width",
    "display.desktop_resolution.height",
    "display.refresh_hz",
})

# Critical for device matching; the rest are optional context supplements.
_CRITICAL = frozenset({"device_form"})
_CRITICAL_GPU_ATTRIBUTE = "raw_name"


@dataclass(frozen=True)
class ProbeReading:
    field_path: str
    status: str  # "succeeded" | "failed"
    raw: bytes
    text: str | None = None
    error_code: str | None = None


class ProbeAdapter(Protocol):
    def probe(self, field_paths: Sequence[str], *, timeout: float) -> list[ProbeReading]: ...


def is_probe_path(field_path: str) -> bool:
    if field_path in _SCALAR_PATHS:
        return True
    match = _GPU_PATH.match(field_path)
    return match is not None and int(match.group(1)) <= 15


def is_critical(field_path: str) -> bool:
    if field_path in _CRITICAL:
        return True
    match = _GPU_PATH.match(field_path)
    return match is not None and match.group(2) == _CRITICAL_GPU_ATTRIBUTE


class DeviceService:
    def __init__(
        self,
        repository: ProductRepository,
        files: FileService | None,
        probe: ProbeAdapter,
        *,
        timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        if files is None:
            # Probe originals are the evidence for every known value; without a
            # managed store the snapshot could not be honest.
            raise ContractError(
                ErrorCode.PRECONDITION_UNVERIFIED,
                "device scans require a managed file store for probe originals",
            )
        self.repository = repository
        self.files = files
        self.probe = probe
        self.timeout = timeout

    # -- task entry point ---------------------------------------------------
    def handle(self, context: TaskContext) -> HandlerOutcome:
        task_input = context.task_input
        if not isinstance(task_input, DeviceScanInput):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the device scan handler received another task type",
            )
        paths = self._validate_paths(task_input.field_paths)
        # Reference errors are rejected before any probe or managed file exists.
        stored_context = None
        if task_input.game_context_id is not None:
            stored_context = self.repository.find_object(
                "GameContext", task_input.game_context_id
            )
            if stored_context is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the referenced game context does not exist",
                    field_errors=[FieldError(
                        path="game_context_id", code="reference", message="unknown context",
                    )],
                )

        readings = self._read(paths)
        snapshot_id = new_id()
        artifacts = self._store_originals(snapshot_id, readings)
        snapshot, gpus_by_index, field_values = self._assemble(
            snapshot_id, paths, readings, artifacts
        )
        request = self._plan_questions(
            context, task_input, snapshot, readings, gpus_by_index, field_values,
            stored_context,
        )
        # The snapshot and the question about it are one fact: a consumer must
        # never see a snapshot whose failures were not asked about.
        with self.repository.unit_of_work() as uow:
            uow.insert_versioned("DeviceSnapshot", snapshot)
            if request is not None:
                uow.insert_object("InformationRequest", request)

        result_refs = [ObjectRef(entity_type="DeviceSnapshot", entity_id=snapshot.id)]
        if request is not None:
            result_refs.append(
                ObjectRef(entity_type="InformationRequest", entity_id=request.id)
            )

        critical_unknown = [
            path for path in paths
            if is_critical(path)
            and not self._is_known(snapshot, path, gpus_by_index, field_values)
        ]
        if critical_unknown and request is not None:
            # The player must answer before device matching can proceed; the
            # snapshot and question are already durable.
            context.suspend(
                information_request_id=request.id, result_refs=result_refs
            )

        known = [
            path for path in paths
            if self._is_known(snapshot, path, gpus_by_index, field_values)
        ]
        if not known:
            code = "unavailable"
            state = TaskState.failed
        elif len(known) < len(paths):
            code = "limited"
            state = TaskState.partial
        else:
            code = "available"
            state = TaskState.completed
        return HandlerOutcome(
            state=state,
            domain_result=DomainResult(
                domain=TaskType.device_scan,
                code=code,
                input_revision=context.task_input_revision,
                checks=list(snapshot.checks),
            ),
            result_refs=result_refs,
        )

    # -- validation ---------------------------------------------------------
    @staticmethod
    def _validate_paths(field_paths: Sequence[str]) -> list[str]:
        if not field_paths:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a device scan needs at least one controlled field path",
                field_errors=[FieldError(path="field_paths", code="required", message="empty")],
            )
        invalid = [path for path in field_paths if not is_probe_path(path)]
        if invalid:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "unregistered device field paths were requested",
                field_errors=[
                    FieldError(path="field_paths", code="enum", message=f"unknown path: {path}")
                    for path in invalid
                ],
            )
        duplicates = {path for path in field_paths if list(field_paths).count(path) > 1}
        if duplicates:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a field path is requested more than once",
                field_errors=[FieldError(path="field_paths", code="conflict", message="duplicate")],
            )
        return list(field_paths)

    # -- probing ------------------------------------------------------------
    def _read(self, paths: Sequence[str]) -> list[ProbeReading]:
        try:
            readings = list(self.probe.probe(paths, timeout=self.timeout))
        except TimeoutError:
            # A whole-probe timeout is a failure for every requested path.
            return [
                ProbeReading(
                    field_path=path, status="failed", raw=b"", error_code="PROBE_TIMEOUT",
                )
                for path in paths
            ]
        by_path = {reading.field_path: reading for reading in readings}
        missing = [path for path in paths if path not in by_path]
        return list(readings) + [
            ProbeReading(
                field_path=path, status="failed", raw=b"", error_code="PROBE_UNAVAILABLE",
            )
            for path in missing
        ]

    def _store_originals(
        self, snapshot_id: str, readings: Sequence[ProbeReading]
    ) -> dict[str, Any]:
        originals: dict[str, Any] = {}
        for reading in readings:
            data = reading.raw or (reading.error_code or "PROBE_FAILED").encode("utf-8")
            originals[reading.field_path] = self.files.store_artifact(
                data,
                relative_name=f"probe/{snapshot_id}/{reading.field_path.replace('.', '_')}.txt",
                stable_key=f"probe:{snapshot_id}:{reading.field_path}",
            )
        return originals

    # -- snapshot assembly ---------------------------------------------------
    def _assemble(
        self,
        snapshot_id: str,
        paths: Sequence[str],
        readings: Sequence[ProbeReading],
        originals: dict[str, Any],
    ) -> tuple[DeviceSnapshot, dict[int, GpuSnapshot], dict[str, FieldValue]]:
        by_path = {reading.field_path: reading for reading in readings}
        captured = utc_now()
        # Every probed path keeps its own value state: a composite such as the
        # desktop resolution must not decide whether its halves were obtained.
        field_values: dict[str, FieldValue] = {}

        def value_for(path: str, parser) -> FieldValue:
            value = self._field_value(path, by_path.get(path), originals.get(path), parser)
            field_values[path] = value
            return value

        gpu_indices = sorted({
            int(_GPU_PATH.match(path).group(1))
            for path in paths
            if _GPU_PATH.match(path) is not None
        })
        gpus: list[GpuSnapshot] = []
        # The snapshot list is compacted, so keep the probe index alongside it:
        # gpus.1.* must never be answered by the list's first entry.
        gpus_by_index: dict[int, GpuSnapshot] = {}
        for index in gpu_indices:
            raw_name = value_for(f"gpus.{index}.raw_name", _parse_text)
            if raw_name.value_state is not ValueState.known:
                # No adapter member is registered without a known name; the
                # failure stays visible in checks and questions.
                continue
            gpu = GpuSnapshot(
                id=new_id(),
                raw_name=raw_name,
                vram_bytes=value_for(f"gpus.{index}.vram_bytes", _parse_positive_int),
                driver_version=value_for(f"gpus.{index}.driver_version", _parse_text),
            )
            gpus.append(gpu)
            gpus_by_index[index] = gpu

        width = value_for("display.desktop_resolution.width", _parse_positive_int)
        height = value_for("display.desktop_resolution.height", _parse_positive_int)
        if (
            width.value_state is ValueState.known
            and height.value_state is ValueState.known
            and width.value > 0
            and height.value > 0
        ):
            resolution = FieldValue[Resolution](
                value_state=ValueState.known,
                value=Resolution(width=width.value, height=height.value),
                acquisition_status=AcquisitionStatus.succeeded,
                provenance=list(width.provenance),
            )
        else:
            resolution = FieldValue[Resolution](
                value_state=ValueState.unknown,
                value=None,
                unknown_reason="not_provided",
                acquisition_status=AcquisitionStatus.failed
                if width.acquisition_status is AcquisitionStatus.failed
                or height.acquisition_status is AcquisitionStatus.failed
                else AcquisitionStatus.not_attempted,
            )

        checks = [
            self._probe_check(path, by_path.get(path), originals.get(path))
            for path in paths
        ]
        if not gpu_indices:
            # An empty GPU list must never read as "this machine has no GPU":
            # record that the group was not examined in this scan.
            checks.append(CheckResult(
                check_id="probe.gpus",
                definition_version="probe-checks/0.4",
                status=CheckStatus.not_run,
                input_refs=[],
                evidence_refs=[],
                checked_at=None,
                method="controlled probe",
                note="no GPU field was requested; the empty list is not a finding",
            ))
        return DeviceSnapshot(
            id=snapshot_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=captured,
            created_by_task_id=None,
            series_id=new_id(),
            version=1,
            device_series_id=new_id(),
            captured_at=captured,
            gpus=gpus,
            selected_gpu_id=None,
            cpu=value_for("cpu", _parse_text),
            memory_bytes=value_for("memory_bytes", _parse_positive_int),
            device_form=value_for("device_form", _parse_text),
            os_version=value_for("os_version", _parse_text),
            display=DisplayContext(
                desktop_resolution=resolution,
                refresh_hz=value_for("display.refresh_hz", _parse_positive_float),
            ),
            power_context={},
            checks=checks,
        ), gpus_by_index, field_values

    def _field_value(self, path: str, reading, original, parser) -> FieldValue:
        if reading is None:
            return FieldValue(
                value_state=ValueState.unknown,
                value=None,
                unknown_reason="not_provided",
                acquisition_status=AcquisitionStatus.not_attempted,
            )
        evidence = self._evidence(original, path) if original is not None else None
        attempt = AcquisitionAttempt(
            id=new_id(),
            method=AcquisitionMethod.system_probe,
            status=AcquisitionStatus.succeeded
            if reading.status == "succeeded"
            else AcquisitionStatus.failed,
            attempted_at=utc_now(),
            error_code=reading.error_code,
        )
        if reading.status != "succeeded":
            return FieldValue(
                value_state=ValueState.unknown,
                value=None,
                unknown_reason="not_provided",
                provenance=[self._provenance(evidence, "probe failed; no value was returned")],
                acquisition_status=AcquisitionStatus.failed,
                attempts=[attempt],
            )
        parsed = parser(reading.text)
        if parsed is None:
            failed_attempt = attempt.model_copy(
                update={"status": AcquisitionStatus.failed, "error_code": "PROBE_PARSE_FAILED"}
            )
            return FieldValue(
                value_state=ValueState.unknown,
                value=None,
                unknown_reason="parse_failed",
                provenance=[self._provenance(evidence, "probe output could not be parsed")],
                acquisition_status=AcquisitionStatus.failed,
                attempts=[failed_attempt],
            )
        return FieldValue(
            value_state=ValueState.known,
            value=parsed,
            provenance=[self._provenance(evidence, "read-only probe output")],
            acquisition_status=AcquisitionStatus.succeeded,
            attempts=[attempt],
        )

    def _probe_check(self, path: str, reading, original) -> CheckResult:
        if reading is None:
            return CheckResult(
                check_id=f"probe.{path}",
                definition_version="probe-checks/0.4",
                status=CheckStatus.not_run,
                input_refs=[],
                evidence_refs=[],
                checked_at=None,
                method="controlled probe",
                note="the adapter returned no reading for this path",
            )
        return CheckResult(
            check_id=f"probe.{path}",
            definition_version="probe-checks/0.4",
            status=CheckStatus.pass_ if reading.status == "succeeded" else CheckStatus.fail,
            input_refs=[],
            evidence_refs=[self._evidence(original, path)] if original is not None else [],
            checked_at=utc_now(),
            method="controlled probe",
            note=reading.error_code or "probe completed",
        )

    def _provenance(self, evidence: EvidenceRef | None, note: str) -> Provenance:
        return Provenance(
            method=AcquisitionMethod.system_probe,
            observed_at=None,
            recorded_at=utc_now(),
            evidence_refs=[evidence] if evidence is not None else [],
            note=note,
        )

    def _evidence(self, file_id: str, path: str) -> EvidenceRef:
        stored = self.repository.get_object("ManagedFile", file_id)
        return EvidenceRef(
            kind="artifact",
            file_id=file_id,
            content_hash=stored.payload["manifest"]["sha256"],
            locator=stored.payload["relative_path"],
        )

    # -- questions ----------------------------------------------------------
    def _plan_questions(
        self,
        context: TaskContext,
        task_input: DeviceScanInput,
        snapshot: DeviceSnapshot,
        readings: Sequence[ProbeReading],
        gpus_by_index: dict[int, GpuSnapshot],
        field_values: dict[str, FieldValue],
        stored_context: Any,
    ) -> InformationRequest | None:
        unknown_paths = [
            reading.field_path
            for reading in readings
            if not self._is_known(
                snapshot, reading.field_path, gpus_by_index, field_values
            )
        ]
        if not unknown_paths:
            return None
        fields = [
            RequestedField(
                field_path=path,
                purpose=f"device field {path} could not be obtained",
                required_for=["device.matching"] if is_critical(path) else [],
                candidates=[],
                answer_state=AnswerState.unanswered,
                value=FieldValue(
                    value_state=ValueState.unknown,
                    value=None,
                    unknown_reason="not_provided",
                    acquisition_status=AcquisitionStatus.not_attempted,
                ),
            )
            for path in sorted(unknown_paths)
        ]
        context_refs: list[VersionRef] = []
        if stored_context is not None:
            context_refs.append(VersionRef(
                entity_type="GameContext",
                entity_id=stored_context.object_id,
                series_id=stored_context.series_id,
                version=stored_context.version,
            ))
        # The question is about this exact device snapshot, not the latest one.
        context_refs.append(VersionRef(
            entity_type="DeviceSnapshot",
            entity_id=snapshot.id,
            series_id=snapshot.series_id,
            version=snapshot.version,
        ))
        return InformationRequest(
            id=new_id(),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=utc_now(),
            created_by_task_id=None,
            source_task_id=context.task_id,
            version=1,
            context_refs=context_refs,
            expected_context_revision=1,
            fields=fields,
            state=InformationRequestState.open,
            result_refs=[],
            superseded_by_request_id=None,
            continuation_mode=InformationContinuationMode.source_task,
            amends_request_id=None,
            amendment_root_ref=None,
        )

    @staticmethod
    def _is_known(
        snapshot: DeviceSnapshot,
        path: str,
        gpus_by_index: dict[int, GpuSnapshot],
        field_values: dict[str, FieldValue],
    ) -> bool:
        if path in ("cpu", "memory_bytes", "os_version", "device_form"):
            return getattr(snapshot, path).value_state is ValueState.known
        if path in (
            "display.desktop_resolution.width",
            "display.desktop_resolution.height",
            "display.refresh_hz",
        ):
            # Each axis is judged on its own probe result, not on the composite.
            value = field_values.get(path)
            return value is not None and value.value_state is ValueState.known
        match = _GPU_PATH.match(path)
        if match is not None:
            index, attribute = int(match.group(1)), match.group(2)
            gpu = gpus_by_index.get(index)
            if gpu is None:
                return False
            return getattr(gpu, attribute).value_state is ValueState.known
        return False


def _parse_text(text: str | None) -> str | None:
    if text is None:
        return None
    value = text.strip()
    return value or None


def _parse_int(text: str | None) -> int | None:
    if text is None:
        return None
    value = text.strip()
    if not value.isdigit():
        # Units are never guessed: a value like "16 GB" stays unknown.
        return None
    return int(value)


def _parse_positive_int(text: str | None) -> int | None:
    value = _parse_int(text)
    return value if value is not None and value > 0 else None


def _parse_positive_float(text: str | None) -> float | None:
    value = _parse_float(text)
    return value if value is not None and value > 0 else None


def _parse_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.strip())
    except ValueError:
        return None


__all__ = [
    "DeviceService",
    "ProbeAdapter",
    "ProbeReading",
    "is_probe_path",
    "is_critical",
]
