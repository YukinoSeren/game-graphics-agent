"""Read-only device acquisition, source wrapping and failure questions (01).

The service turns probe readings into a versioned ``DeviceSnapshot`` where every
known value cites a published original.  A failed probe never becomes a default
value: it becomes an unknown field plus a question, and an empty GPU list is
always explained by a failed check.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence
from uuid import UUID, uuid5

from game_app.contracts.common import (
    AcquisitionAttempt,
    CheckResult,
    EvidenceRef,
    FieldValue,
    MutationContext,
    Provenance,
    ResourceResult,
)
from game_app.contracts.device import (
    AdapterCapabilities,
    CreateInstallationRequest,
    DeviceSnapshot,
    DisplayContext,
    GameContext,
    GameInstallation,
    GpuSnapshot,
    InstallationCatalogQuery,
    InstallationChoice,
    InstallationChoicePage,
    InstallationRegistration,
    LocalSettingChoice,
    LocalSettingRegistration,
    RegisterLocalSettingRequest,
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
from game_app.contracts.knowledge import Game
from game_app.contracts.primitives import ObjectRef, VersionRef, new_id, utc_now
from game_app.contracts.tasks import DeviceScanInput, DomainResult
from game_app.errors import ContractError, ErrorCode, FieldError, empty_details
from game_app.files import FileService, PublishHooks
from game_app.repository import ProductRepository, UnitOfWork, canonical_json
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
                probe_index=index,
                raw_name=raw_name,
                vram_bytes=value_for(f"gpus.{index}.vram_bytes", _parse_positive_int),
                driver_version=value_for(f"gpus.{index}.driver_version", _parse_text),
            )
            gpus.append(gpu)
            gpus_by_index[index] = gpu

        width = value_for("display.desktop_resolution.width", _parse_positive_int)
        height = value_for("display.desktop_resolution.height", _parse_positive_int)
        axes: dict[str, FieldValue[int]] = {}
        if "display.desktop_resolution.width" in paths:
            axes["width"] = width
        if "display.desktop_resolution.height" in paths:
            axes["height"] = height
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
                desktop_resolution_axes=axes,
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


# -- D-P01/D-P02: installations and the local setting directory --------------


INSTALLATION_ROUTE = "/api/installations"
LOCAL_SETTING_ROUTE = "/api/local-settings"
INSTALLATION_FILE = "installations/registration.json"
LOCAL_SETTING_FILE = "local-settings/registration.json"
INSTALLATION_RULE_VERSION = "installation-registration/0.4"
LOCAL_SETTING_RULE_VERSION = "local-setting-registration/0.4"
PLAYER_NAMESPACE = "player"
_ID_NAMESPACE = UUID("2c7f6f4a-1c2b-4f5d-9c55-6e0a7d1b3f21")


def _deterministic_id(*parts: str) -> str:
    """An id that is stable for one (submission, role) pair.

    A same-key retry must reproduce the ids it wrote into its declaration
    manifest; a crash between publishing and committing then reuses them
    instead of preparing a second, different declaration.
    """
    return uuid5(_ID_NAMESPACE, ":".join(parts)).hex


class ContextService:
    """Installation registration, context preparation and the local catalog.

    Nothing here probes a disk, reads a game file or adopts a value: an entry
    names a menu position, and a context records what the player declared.
    """

    def __init__(self, repository: ProductRepository, files: FileService) -> None:
        self.repository = repository
        self.files = files

    # -- context ------------------------------------------------------------
    def prepare_game_context(
        self,
        game_revision_id: str,
        platform: str | None,
        installation_id: str | None,
        base_context_id: str | None = None,
        *,
        declaration_ref: EvidenceRef | None = None,
    ) -> GameContext:
        """Return a context candidate; it is never committed and never probes.

        A fixed base is only legal for the same game series and installation.
        It carries the declared build/process background forward; it never
        supplies installation or device projections.  A platform that is not
        inherited from that base needs the evidence reference of its own
        declaration — no value is marked known without one.
        """
        game = self._game(game_revision_id)
        base = None
        if base_context_id is not None:
            stored = self.repository.find_object("GameContext", base_context_id)
            if stored is None:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the base context does not exist",
                    field_errors=[FieldError(
                        path="base_context_id", code="reference", message="unknown context",
                    )],
                )
            base = GameContext.model_validate_json(stored.payload_bytes)
            if base.game_id != game.series_id or base.installation_id != installation_id:
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    "a base context must belong to the same game series and installation",
                    field_errors=[FieldError(
                        path="base_context_id", code="conflict",
                        message="another game series or installation",
                    )],
                )

        if platform is None:
            platform_value = base.platform if base is not None else _unknown_platform()
        elif (
            base is not None
            and base.platform.value_state is ValueState.known
            and base.platform.value == platform
        ):
            # The declaration behind this value is unchanged; reuse its evidence.
            platform_value = base.platform
        elif declaration_ref is not None:
            platform_value = _declared_platform(platform, declaration_ref)
        else:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a declared platform needs its declaration evidence or a fixed base context",
                field_errors=[FieldError(
                    path="platform", code="required",
                    message="pass declaration_ref or base_context_id",
                )],
            )

        return GameContext(
            id=new_id(),
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=utc_now(),
            created_by_task_id=None,
            series_id=new_id(),
            version=1,
            game_id=game.series_id,
            game_revision_id=game.id,
            installation_id=installation_id,
            platform=platform_value,
            build_identifiers=list(base.build_identifiers) if base else [],
            process_candidates=list(base.process_candidates) if base else [],
            selected_process=base.selected_process if base else None,
            adapter_id=None,
            adapter_version=None,
            capabilities=_no_adapter_capabilities(),
            checks=list(base.checks) if base else [_unknown_context_check("context.build")],
        )

    # -- installations ------------------------------------------------------
    def create_installation(
        self,
        request: CreateInstallationRequest,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": "POST",
            "route": INSTALLATION_ROUTE,
            "request": request.model_dump(mode="json"),
        })).hexdigest()
        # Reject a bad reference before preparing a declaration file.  A key
        # that already has a registered response replays it instead, so its
        # conflict/duplicate handling stays with publish.
        if not self._has_registered_submission(context, INSTALLATION_ROUTE):
            self._validate_source(
                request.game_context_id,
                self.repository.find_object("GameContext", request.game_context_id),
            )
        installation_id = _deterministic_id(
            context.user_scope_id, INSTALLATION_ROUTE, context.idempotency_key,
            request_hash, "installation",
        )
        game_context_id = _deterministic_id(
            context.user_scope_id, INSTALLATION_ROUTE, context.idempotency_key,
            request_hash, "context",
        )
        registration_id = _deterministic_id(
            context.user_scope_id, INSTALLATION_ROUTE, context.idempotency_key,
            request_hash, "registration",
        )
        display_label = _display_label(request.display_label, installation_id)
        manifest = {
            "kind": "installation_registration",
            "rule_version": INSTALLATION_RULE_VERSION,
            "request": request.model_dump(mode="json"),
            "installation_id": installation_id,
            "game_context_id": game_context_id,
            "display_label": display_label,
        }
        published = self.files.publish(
            kind="import",
            files={INSTALLATION_FILE: canonical_json(manifest)},
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=INSTALLATION_ROUTE,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_installation(
                uow,
                request=request,
                user_scope_id=context.user_scope_id,
                installation_id=installation_id,
                game_context_id=game_context_id,
                registration_id=registration_id,
                managed=managed,
            ),
            hooks=hooks,
        )
        return ResourceResult(
            refs=[ObjectRef.model_validate(ref) for ref in published.refs]
        )

    def _write_installation(
        self,
        uow: UnitOfWork,
        *,
        request: CreateInstallationRequest,
        user_scope_id: str,
        installation_id: str,
        game_context_id: str,
        registration_id: str,
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        source = self._validate_source(
            request.game_context_id,
            uow.find_object("GameContext", request.game_context_id),
            uow=uow,
        )

        installation = GameInstallation(
            id=installation_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=utc_now(),
            created_by_task_id=None,
            series_id=installation_id,
            version=1,
            revision=1,
            user_scope_id=user_scope_id,
            game_id=source.game_id,
            origin="user_provided",
            platform=source.platform,
            private_path_ref=None,
        )
        # The player background (declared platform and any declared build or
        # process facts) is carried forward; no adapter or device projection is.
        new_context = GameContext(
            id=game_context_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=utc_now(),
            created_by_task_id=None,
            series_id=game_context_id,
            version=1,
            game_id=source.game_id,
            game_revision_id=source.game_revision_id,
            installation_id=installation_id,
            platform=source.platform,
            build_identifiers=list(source.build_identifiers),
            process_candidates=list(source.process_candidates),
            selected_process=source.selected_process,
            adapter_id=None,
            adapter_version=None,
            capabilities=_no_adapter_capabilities(),
            checks=list(source.checks),
        )
        registration = InstallationRegistration(
            id=registration_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=utc_now(),
            created_by_task_id=None,
            input=request,
            installation_id=installation_id,
            game_context_id=game_context_id,
            declaration_file_id=managed[0].id,
        )
        uow.insert_versioned("GameInstallation", installation)
        uow.insert_versioned("GameContext", new_context)
        uow.insert_object("InstallationRegistration", registration)
        installation_ref = ObjectRef(entity_type="GameInstallation", entity_id=installation_id)
        context_ref = ObjectRef(entity_type="GameContext", entity_id=game_context_id)
        uow.insert_reference(registration_id, "installation", installation_ref)
        uow.insert_reference(registration_id, "game_context", context_ref)
        return [
            ObjectRef(entity_type="InstallationRegistration", entity_id=registration_id),
            installation_ref,
            context_ref,
        ]

    def list_installations(
        self,
        query: InstallationCatalogQuery,
        *,
        user_scope_id: str,
    ) -> InstallationChoicePage:
        with self.repository.unit_of_work() as uow:
            installations = [
                stored.payload
                for stored in uow.latest_objects("GameInstallation")
                if stored.payload.get("user_scope_id") == user_scope_id
                and stored.payload.get("game_id") == query.game_id
            ]
            registrations = {
                payload["installation_id"]: payload
                for payload in (
                    stored.payload for stored in uow.iter_objects("InstallationRegistration")
                )
                if payload.get("installation_id") in {
                    item["series_id"] for item in installations
                }
            }
        installations.sort(key=lambda item: (item["created_at"], item["series_id"]))
        start = 0
        if query.cursor:
            ids = [item["series_id"] for item in installations]
            if query.cursor not in ids:
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    "the installation cursor is no longer valid; re-read the catalog",
                )
            start = ids.index(query.cursor) + 1
        selected = installations[start:start + query.limit]
        choices: list[InstallationChoice] = []
        for installation in selected:
            registration = registrations.get(installation["series_id"])
            if registration is None:
                # Every installation is registered together with its fixed
                # context; without that record no honest choice can be built.
                continue
            choices.append(InstallationChoice(
                installation_id=installation["series_id"],
                revision=installation["revision"],
                display_label=_display_label(
                    registration["input"]["display_label"], installation["series_id"]
                ),
                game_context_id=registration["game_context_id"],
            ))
        next_cursor = None
        if start + query.limit < len(installations):
            next_cursor = installations[start + query.limit - 1]["series_id"]
        return InstallationChoicePage(choices=choices, next_cursor=next_cursor)

    # -- local setting directory -------------------------------------------
    def register_local_setting(
        self,
        request: RegisterLocalSettingRequest,
        context: MutationContext,
        *,
        request_schema_version: str = PRODUCT_CONTRACT_VERSION,
        hooks: PublishHooks | None = None,
    ) -> ResourceResult:
        _validate_local_setting_request(request)
        request_hash = hashlib.sha256(canonical_json({
            "schema_version": request_schema_version,
            "method": "POST",
            "route": LOCAL_SETTING_ROUTE,
            "request": request.model_dump(mode="json"),
        })).hexdigest()

        def ids(role: str) -> str:
            return _deterministic_id(
                context.user_scope_id, LOCAL_SETTING_ROUTE, context.idempotency_key,
                request_hash, role,
            )

        registered = self._has_registered_submission(context, LOCAL_SETTING_ROUTE)
        plan = None
        if not registered:
            installation = self._installation_for(
                request.installation_id, context.user_scope_id
            )
            plan = self._plan_local_setting(installation.series_id, request, ids)
            installation_id = installation.series_id
        else:
            installation_id = request.installation_id
        manifest = {
            "kind": "local_setting_registration",
            "rule_version": LOCAL_SETTING_RULE_VERSION,
            "request": request.model_dump(mode="json"),
            "installation_id": installation_id,
            "namespace": PLAYER_NAMESPACE,
            "definition_id": None if plan is None else plan.definition_id,
            "creates_definition": None if plan is None else plan.creates_definition,
        }
        registration_id = ids("registration")
        published = self.files.publish(
            kind="import",
            files={LOCAL_SETTING_FILE: canonical_json(manifest)},
            user_scope_id=context.user_scope_id,
            method="POST",
            canonical_route=LOCAL_SETTING_ROUTE,
            key=context.idempotency_key,
            request_schema_version=request_schema_version,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write_local_setting(
                uow,
                request=request,
                plan=plan,
                user_scope_id=context.user_scope_id,
                registration_id=registration_id,
                managed=managed,
            ),
            hooks=hooks,
        )
        return ResourceResult(
            refs=[ObjectRef.model_validate(ref) for ref in published.refs]
        )

    def _write_local_setting(
        self,
        uow: UnitOfWork,
        *,
        request: RegisterLocalSettingRequest,
        plan: "_LocalSettingPlan",
        user_scope_id: str,
        registration_id: str,
        managed: Sequence[Any],
    ) -> list[ObjectRef]:
        if plan is None:  # pragma: no cover - only a replayed submission has none
            raise ContractError(
                ErrorCode.INTERNAL_ERROR, "a new submission must plan its local setting"
            )
        installation = self._installation_for(
            request.installation_id, user_scope_id, uow=uow
        )
        self._recheck_local_setting(uow, plan, request, installation.series_id)

        if plan.creates_definition:
            uow.register_local_setting(
                installation_id=installation.series_id,
                namespace=PLAYER_NAMESPACE,
                raw_name=request.raw_name,
                menu_path=request.menu_path,
                local_setting_id=plan.definition_id,
                series_id=plan.series_id,
                version=plan.version,
                parent_definition_id=plan.parent_definition_id,
            )

        registration = LocalSettingRegistration(
            id=registration_id,
            schema_version=PRODUCT_CONTRACT_VERSION,
            created_at=utc_now(),
            created_by_task_id=None,
            input=request,
            definition_id=plan.definition_id,
            declaration_file_id=managed[0].id,
        )
        uow.insert_object("LocalSettingRegistration", registration)
        definition_ref = ObjectRef(
            entity_type="LocalSettingDefinition", entity_id=plan.definition_id
        )
        file_ref = ObjectRef(entity_type="ManagedFile", entity_id=managed[0].id)
        uow.insert_reference(registration_id, "definition", definition_ref)
        return [
            ObjectRef(entity_type="LocalSettingRegistration", entity_id=registration_id),
            definition_ref,
            file_ref,
        ]

    def list_local_settings(
        self, installation_id: str, *, user_scope_id: str
    ) -> list[LocalSettingChoice]:
        installation = self._installation_for(installation_id, user_scope_id)
        return [
            LocalSettingChoice(
                definition_id=head.local_setting_id,
                series_id=head.series_id,
                raw_name=head.raw_name,
                menu_path=list(head.menu_path),
                location_known=head.located,
            )
            for head in self.repository.list_local_setting_heads(installation.series_id)
        ]

    # -- helpers -------------------------------------------------------------
    def _has_registered_submission(
        self, context: MutationContext, route: str
    ) -> bool:
        return self.repository.find_idempotency(
            context.user_scope_id, "POST", route, context.idempotency_key
        ) is not None

    def _validate_source(
        self, game_context_id: str, stored: Any, *, uow: UnitOfWork | None = None
    ) -> GameContext:
        """A registration only references an existing, self-consistent context."""
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced game context does not exist",
                field_errors=[FieldError(
                    path="game_context_id", code="reference", message="unknown context",
                )],
            )
        source = GameContext.model_validate_json(stored.payload_bytes)
        game_stored = (
            uow.find_object("Game", source.game_revision_id)
            if uow is not None
            else self.repository.find_object("Game", source.game_revision_id)
        )
        if game_stored is None or game_stored.payload.get("series_id") != source.game_id:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the context's game revision does not belong to its game",
            )
        return source

    def _game(self, game_revision_id: str) -> Game:
        stored = self.repository.find_object("Game", game_revision_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the game revision does not exist",
                field_errors=[FieldError(
                    path="game_revision_id", code="reference", message="unknown game revision",
                )],
            )
        return Game.model_validate_json(stored.payload_bytes)

    def _installation_for(
        self, installation_id: str, user_scope_id: str, *, uow: UnitOfWork | None = None
    ) -> GameInstallation:
        if uow is None:
            with self.repository.unit_of_work() as work:
                stored = work.latest_for_series("GameInstallation", installation_id)
        else:
            stored = uow.latest_for_series("GameInstallation", installation_id)
        if stored is None or json.loads(stored.payload_bytes).get("user_scope_id") != user_scope_id:
            # Another user's installation is indistinguishable from a missing one.
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND, "the installation is not available"
            )
        return GameInstallation.model_validate_json(stored.payload_bytes)

    def _plan_local_setting(
        self,
        installation_id: str,
        request: RegisterLocalSettingRequest,
        ids: Callable[[str], str],
    ) -> "_LocalSettingPlan":
        heads = {
            head.local_setting_id: head
            for head in self.repository.list_local_setting_heads(installation_id)
        }
        if request.identity_mode == "auto":
            slot = self.repository.get_local_setting_slot(
                installation_id, PLAYER_NAMESPACE, request.raw_name, request.menu_path
            )
            if slot is not None:
                head = next(
                    (item for item in heads.values() if item.series_id == slot), None
                )
                if head is None:
                    raise ContractError(
                        ErrorCode.LOCAL_SETTING_CONFLICT,
                        "the located slot has no current definition; re-read the catalog",
                    )
                return _LocalSettingPlan(
                    definition_id=head.local_setting_id,
                    series_id=head.series_id,
                    version=head.version,
                    parent_definition_id=None,
                    creates_definition=False,
                    mode="reuse-existing",
                )
            return self._new_plan(ids)

        if request.identity_mode == "distinct":
            return self._new_plan(ids)

        target = heads.get(request.reuse_local_setting_id or "")
        if target is None:
            self._refuse_foreign_or_stale(installation_id, request.reuse_local_setting_id)
            raise AssertionError("unreachable: _refuse_foreign_or_stale always raises")
        if request.supersedes_local_setting_id is None:
            if target.raw_name != request.raw_name or tuple(request.menu_path) != target.menu_path:
                raise ContractError(
                    ErrorCode.INPUT_VALIDATION_FAILED,
                    "an explicit reuse must match the definition's raw name and menu path exactly",
                    field_errors=[FieldError(
                        path="reuse_local_setting_id", code="conflict",
                        message="name or path differs from the referenced definition",
                    )],
                )
            return _LocalSettingPlan(
                definition_id=target.local_setting_id,
                series_id=target.series_id,
                version=target.version,
                parent_definition_id=None,
                creates_definition=False,
                mode="reuse-existing",
            )
        # A correction always targets the current head of its own series.
        if target.local_setting_id != request.supersedes_local_setting_id:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "supersedes_local_setting_id and reuse_local_setting_id must be the same definition",
            )
        if (
            target.raw_name == request.raw_name
            and tuple(request.menu_path) == target.menu_path
        ):
            return _LocalSettingPlan(
                definition_id=target.local_setting_id,
                series_id=target.series_id,
                version=target.version,
                parent_definition_id=None,
                creates_definition=False,
                mode="reuse-existing",
            )
        return _LocalSettingPlan(
            definition_id=ids("definition:correction"),
            series_id=target.series_id,
            version=target.version + 1,
            parent_definition_id=target.local_setting_id,
            creates_definition=True,
            mode="correction",
        )

    @staticmethod
    def _new_plan(ids: Callable[[str], str]) -> "_LocalSettingPlan":
        return _LocalSettingPlan(
            definition_id=ids("definition"),
            series_id=ids("series"),
            version=1,
            parent_definition_id=None,
            creates_definition=True,
            mode="new",
        )

    def _refuse_foreign_or_stale(
        self, installation_id: str, definition_id: str | None
    ) -> None:
        if definition_id is None:
            return
        for stored in self.repository_iter_definitions():
            payload = stored.payload
            if payload.get("id") != definition_id:
                continue
            if payload.get("installation_id") != installation_id:
                raise ContractError(
                    ErrorCode.REFERENCE_INVALID,
                    "the referenced definition belongs to another installation",
                    field_errors=[FieldError(
                        path="reuse_local_setting_id", code="reference",
                        message="cross-installation definition",
                    )],
                )
            with self.repository.unit_of_work() as uow:
                successor = uow.query(
                    "SELECT child_id FROM local_setting_successors WHERE parent_id=?",
                    (definition_id,),
                )
            successor_id = successor[0]["child_id"] if successor else None
            raise ContractError(
                ErrorCode.LOCAL_SETTING_CONFLICT,
                "the referenced definition is no longer the current head"
                + (f"; continue from {successor_id}" if successor_id else ""),
                details=empty_details(
                    current_resource_ref=(
                        None
                        if successor_id is None
                        else ObjectRef(
                            entity_type="LocalSettingDefinition", entity_id=successor_id
                        )
                    ),
                ),
            )
        raise ContractError(
            ErrorCode.REFERENCE_INVALID,
            "the referenced definition does not exist",
            field_errors=[FieldError(
                path="reuse_local_setting_id", code="reference", message="unknown definition",
            )],
        )

    def repository_iter_definitions(self):
        with self.repository.unit_of_work() as uow:
            return uow.iter_objects("LocalSettingDefinition")

    def _recheck_local_setting(
        self,
        uow: UnitOfWork,
        plan: "_LocalSettingPlan",
        request: RegisterLocalSettingRequest,
        installation_id: str,
    ) -> None:
        heads = {
            head.local_setting_id: head
            for head in uow.list_local_setting_heads(installation_id)
        }
        if request.identity_mode == "auto" and plan.creates_definition:
            slot = uow.get_local_setting_slot(
                installation_id, PLAYER_NAMESPACE, request.raw_name, request.menu_path
            )
            if slot is not None:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "another registration claimed this location; start a new submission",
                )
        elif plan.mode == "reuse-existing" and request.identity_mode == "auto":
            slot = uow.get_local_setting_slot(
                installation_id, PLAYER_NAMESPACE, request.raw_name, request.menu_path
            )
            head = heads.get(plan.definition_id)
            if slot is None or head is None or head.series_id != plan.series_id:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the located definition changed; re-read the catalog",
                )
        elif request.identity_mode == "reuse":
            head = heads.get(request.reuse_local_setting_id or "")
            if head is None:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the referenced definition is no longer the current head",
                )
            if plan.creates_definition and head.version != plan.version - 1:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the definition changed; re-read the catalog before correcting it",
                )
            if plan.creates_definition and head.local_setting_id != plan.parent_definition_id:
                raise ContractError(
                    ErrorCode.LOCAL_SETTING_CONFLICT,
                    "the correction target changed; re-read the catalog",
                )


@dataclass(frozen=True)
class _LocalSettingPlan:
    definition_id: str
    series_id: str
    version: int
    parent_definition_id: str | None
    creates_definition: bool
    mode: str


def _validate_local_setting_request(request: RegisterLocalSettingRequest) -> None:
    """Reject impossible identity combinations before any file is prepared."""
    if request.identity_mode == "auto":
        if request.menu_path == []:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "automatic identification needs a known menu position",
                field_errors=[FieldError(
                    path="menu_path", code="enum",
                    message="auto is not allowed for an unknown position",
                )],
            )
        if (
            request.reuse_local_setting_id is not None
            or request.supersedes_local_setting_id is not None
        ):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "auto does not take an explicit definition or correction target",
            )
    elif request.identity_mode == "distinct":
        if request.menu_path != []:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "distinct registers an unknown position; a known menu path uses auto",
                field_errors=[FieldError(
                    path="menu_path", code="enum", message="distinct requires an empty menu path",
                )],
            )
        if (
            request.reuse_local_setting_id is not None
            or request.supersedes_local_setting_id is not None
        ):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "distinct does not reference another definition",
            )
    else:
        if request.reuse_local_setting_id is None:
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "reuse requires reuse_local_setting_id",
                field_errors=[FieldError(
                    path="reuse_local_setting_id", code="required", message="missing",
                )],
            )
        if (
            request.supersedes_local_setting_id is not None
            and request.supersedes_local_setting_id != request.reuse_local_setting_id
        ):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a correction must name the same definition it reuses",
            )


def _unknown_platform() -> FieldValue[str]:
    return FieldValue[str](
        value_state=ValueState.unknown,
        value=None,
        unknown_reason="not_provided",
        acquisition_status=AcquisitionStatus.not_attempted,
    )


def _declared_platform(value: str, declaration_ref: EvidenceRef) -> FieldValue[str]:
    return FieldValue[str](
        value_state=ValueState.known,
        value=value,
        acquisition_status=AcquisitionStatus.succeeded,
        provenance=[Provenance(
            method=AcquisitionMethod.user_provided,
            observed_at=None,
            recorded_at=utc_now(),
            evidence_refs=[declaration_ref],
            note="player-declared platform",
        )],
    )


def _no_adapter_capabilities() -> AdapterCapabilities:
    return AdapterCapabilities(
        can_identify=_unknown_context_check("adapter.identify"),
        can_read_config=_unknown_context_check("adapter.read_config"),
        can_bind_capture=_unknown_context_check("adapter.bind_capture"),
    )


def _unknown_context_check(check_id: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version="context-checks/0.4",
        status=CheckStatus.not_run,
        input_refs=[],
        evidence_refs=[],
        checked_at=None,
        method="no verified adapter",
    )


def _display_label(label: str | None, installation_id: str) -> str:
    """A null label is generated for display only; it never identifies an install."""
    return label if label is not None else f"本机记录 {installation_id[:8]}"


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
    "ContextService",
    "DeviceService",
    "INSTALLATION_ROUTE",
    "LOCAL_SETTING_ROUTE",
    "PLAYER_NAMESPACE",
    "ProbeAdapter",
    "ProbeReading",
    "is_probe_path",
    "is_critical",
]
