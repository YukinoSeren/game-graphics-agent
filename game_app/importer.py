"""Freeze a stopped collector run and recognise its format (02a, IMP-T01).

This module is deliberately narrow: it copies the declared members of *one*
caller-supplied directory into managed storage, fingerprints their bytes, and
decides whether the group is a recognised format.  It does **not** publish an
``ImportSnapshot``, map records or references, or build a knowledge package.

Two rules drive the design:

* The source directory is only ever read.  ``mtime`` proves nothing; a group is
  one checkpoint only if its bytes are unchanged across the copy window.
* Format recognition reads top-level structure, ``contract_version`` and the
  required keys.  File names locate candidates, they never decide the format.

Known limitation: the group is read into memory so ``FileService.publish`` can
verify and atomically rename it.  Streaming hashes for very large runs are not
implemented in this slice.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import ValidationError, field_validator

from game_app.contracts.common import CheckResult
from game_app.contracts.enums import (
    CheckStatus,
    LegacyRecordState,
    PublicationStatus,
    TaskState,
    TaskType,
)
from game_app.contracts.knowledge import (
    BoundarySemantics,
    IdentityMapping,
    ImportedExtraction,
    ImportedRecord,
    ImportSnapshot,
    ImportValidation,
    RecordExtension,
    SnapshotFileEntry,
    SnapshotManifest,
    Source,
    SourceMetadata,
    SourceRevision,
)
from game_app.contracts.primitives import Hash, ObjectRef, StrictModel, new_id, utc_now
from game_app.errors import ContractError, ErrorCode, FieldError
from game_app.files import FileEntry, FileManifest, FileService, PublishHooks
from game_app.repository import ProductRepository, UnitOfWork, canonical_json

PRODUCT_CONTRACT_VERSION = "product-contract/0.4"
COLLECTOR_RUN_FORMAT_ID = "collector-run/v0.1"
COLLECTOR_RUN_CONTRACT_VERSION = "0.1"
COLLECTOR_REQUIRED_FILES = (
    "run.json",
    "sources.json",
    "records.json",
    "gaps.json",
    "extractions.json",
)
COLLECTOR_DERIVED_FILES = ("candidates.json", "report.md")
COLLECTOR_DIAGNOSTIC_FILES = ("knowledge.sqlite3",)


class FormatInspection(StrictModel):
    """Local read-only view of one candidate group (02a §5); all fields R."""

    format_id: str | None
    required_files: list[str]
    missing_files: list[str]
    limitations: list[str]


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of asking one format whether it recognises a group.

    ``matched`` is false when the bytes are simply not that format.  When it is
    true, ``limitations`` documents what the group cannot prove and
    ``inconsistencies`` lists findings that must block a freeze.
    """

    matched: bool
    limitations: list[str]
    inconsistencies: list[str]


@dataclass(frozen=True)
class FormatSpec:
    format_id: str
    required_files: tuple[str, ...]
    optional_files: tuple[str, ...]
    check: Callable[[Mapping[str, bytes]], ProbeResult]


@dataclass(frozen=True)
class FreezeHooks:
    """Test-only injection point: runs after the copy, before the commit."""

    after_copy: Callable[[], None] | None = None


@dataclass(frozen=True)
class FrozenGroup:
    """A verified, read-only copy of one stopped run plus its inspection."""

    format_id: str
    operation_id: str
    file_ids: dict[str, str]
    manifest: FileManifest
    inspection: FormatInspection


# --- registry ----------------------------------------------------------------


class FormatRegistry:
    """Ordered format probes over the members a format declares.

    Only declared member names are ever read from a directory; other files are
    neither enumerated nor guessed at.
    """

    def __init__(self, specs: Sequence[FormatSpec] = ()) -> None:
        self._specs = list(specs)

    def register(self, spec: FormatSpec) -> None:
        self._specs.append(spec)

    @property
    def known_files(self) -> tuple[str, ...]:
        names = {
            name
            for spec in self._specs
            for name in (*spec.required_files, *spec.optional_files)
        }
        return tuple(sorted(names))

    @property
    def format_ids(self) -> tuple[str, ...]:
        return tuple(spec.format_id for spec in self._specs)

    def inspect_format(self, directory: str | Path) -> FormatInspection:
        files = read_group(Path(directory), self.known_files)
        return self.inspect_files(files)

    def inspect_files(self, files: Mapping[str, bytes]) -> FormatInspection:
        inspection, _ = self._probe(files)
        return inspection

    def _probe(self, files: Mapping[str, bytes]) -> tuple[FormatInspection, list[str]]:
        present = set(files)
        limitations: list[str] = []
        for spec in self._specs:
            missing = [name for name in spec.required_files if name not in present]
            if missing:
                limitations.append(
                    f"{spec.format_id} is not usable: missing {', '.join(missing)}"
                )
                continue
            result = spec.check(files)
            if not result.matched:
                limitations.extend(result.limitations)
                continue
            inspection = FormatInspection(
                format_id=spec.format_id,
                required_files=list(spec.required_files),
                missing_files=[],
                limitations=[*result.limitations, *result.inconsistencies],
            )
            return inspection, list(result.inconsistencies)

        primary = self._specs[0] if self._specs else None
        inspection = FormatInspection(
            format_id=None,
            required_files=list(primary.required_files) if primary else [],
            missing_files=(
                [name for name in primary.required_files if name not in present]
                if primary
                else []
            ),
            limitations=limitations,
        )
        return inspection, []


def default_format_registry() -> FormatRegistry:
    """The formats this slice may claim to understand.

    Only the offline fixture shape is enabled.  No research/CSV mapper is
    registered here, so no real tool is implied to be supported.
    """

    return FormatRegistry([
        FormatSpec(
            format_id=COLLECTOR_RUN_FORMAT_ID,
            required_files=COLLECTOR_REQUIRED_FILES,
            optional_files=(*COLLECTOR_DERIVED_FILES, *COLLECTOR_DIAGNOSTIC_FILES),
            check=_collector_run_check,
        )
    ])


# --- freezing ----------------------------------------------------------------


class FrozenInputReader:
    """Stable copy and fingerprint of one stopped run directory."""

    def __init__(self, files: FileService, registry: FormatRegistry | None = None) -> None:
        self.files = files
        self.registry = registry or default_format_registry()

    def inspect_format(self, directory: str | Path) -> FormatInspection:
        """Read-only recognition: no model, no probe, no persisted fact."""
        return self.registry.inspect_format(directory)

    def freeze(
        self,
        directory: str | Path,
        *,
        user_scope_id: str,
        idempotency_key: str,
        canonical_route: str = "/api/knowledge-imports",
        method: str = "POST",
        hooks: FreezeHooks | None = None,
    ) -> FrozenGroup:
        directory = Path(directory)
        names = self.registry.known_files
        before = read_group(directory, names)
        if not before:
            raise ContractError(
                ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
                "the directory contains none of the declared group members",
            )

        inspection, inconsistencies = self.registry._probe(before)
        if inspection.format_id is None:
            if inspection.missing_files:
                raise ContractError(
                    ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                    "the file group is missing " + ", ".join(inspection.missing_files),
                )
            raise ContractError(
                ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
                "no registered format matches this structure: "
                + "; ".join(inspection.limitations),
            )
        if inconsistencies:
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                "; ".join(inconsistencies),
            )

        # The group must still match these bytes when the copy starts and again
        # immediately before the publishing transaction commits.
        verify_group_unchanged(directory, names, before)
        request_hash = _freeze_request_hash(before)

        def before_commit() -> None:
            if hooks is not None and hooks.after_copy is not None:
                hooks.after_copy()
            verify_group_unchanged(directory, names, before)

        published = self.files.publish(
            kind="import",
            files=before,
            user_scope_id=user_scope_id,
            method=method,
            canonical_route=canonical_route,
            key=idempotency_key,
            request_schema_version=PRODUCT_CONTRACT_VERSION,
            request_hash=request_hash,
            # Freezing a file group is not publishing an import: no domain
            # writer runs, and "file ready" must not be read as "published".
            writer=None,
            hooks=PublishHooks(before_commit=before_commit),
        )
        operation = self.files.get_operation(published.operation_id)
        file_ids = {entry.relative_name: file_id for entry, file_id in zip(
            operation.manifest.entries, operation.file_ids
        )}
        frozen_bytes = {
            name: self.files.read_managed_file(file_id)
            for name, file_id in file_ids.items()
        }
        frozen_inspection, _ = self.registry._probe(frozen_bytes)
        if frozen_inspection.format_id != inspection.format_id:
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                "the frozen copy does not reproduce the inspected format",
            )
        return FrozenGroup(
            format_id=frozen_inspection.format_id,
            operation_id=published.operation_id,
            file_ids=file_ids,
            manifest=operation.manifest,
            inspection=frozen_inspection,
        )


# --- group reading and verification ------------------------------------------


def read_group(directory: Path, names: Sequence[str]) -> dict[str, bytes]:
    """Read only the declared member names of one directory.

    The directory is never enumerated beyond those names, never recursed into
    and never modified.  A symlink is not followed: the frozen group must be
    bytes that live in the directory the caller named.
    """

    if not directory.is_dir():
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED,
            "the import source must be one existing directory",
        )
    files: dict[str, bytes] = {}
    for name in names:
        path = directory / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            files[name] = path.read_bytes()
        except OSError as error:
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                f"{name} could not be read from the source directory",
            ) from error
    return files


def verify_group_unchanged(
    directory: Path, names: Sequence[str], before: Mapping[str, bytes]
) -> None:
    """Refuse to accept a freeze if the source group no longer matches.

    ``(size, sha256)`` is recomputed for every member; equal mtimes are not
    evidence of an unchanged checkpoint.
    """

    expected = {name: _fingerprint(data) for name, data in before.items()}
    after = {
        name: _fingerprint(data)
        for name, data in read_group(directory, names).items()
    }
    if set(after) != set(expected):
        added = sorted(set(after) - set(expected))
        removed = sorted(set(expected) - set(after))
        detail = ", ".join(
            [*(f"added {name}" for name in added), *(f"removed {name}" for name in removed)]
        )
        raise ContractError(
            ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
            f"the group membership changed during the copy ({detail})",
        )
    changed = sorted(name for name in expected if after[name] != expected[name])
    if changed:
        raise ContractError(
            ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
            "these files changed while the run was being copied: " + ", ".join(changed),
        )


def _fingerprint(data: bytes) -> tuple[int, str]:
    return len(data), hashlib.sha256(data).hexdigest()


def _freeze_request_hash(files: Mapping[str, bytes]) -> str:
    """Content identity of the frozen group, independent of read order."""

    return hashlib.sha256(canonical_json({
        "kind": "import",
        "files": [
            {
                "relative_name": name,
                "byte_length": len(files[name]),
                "sha256": hashlib.sha256(files[name]).hexdigest(),
            }
            for name in sorted(files)
        ],
    })).hexdigest()


# --- collector-run/v0.1 probe -------------------------------------------------


def _collector_run_check(files: Mapping[str, bytes]) -> ProbeResult:
    parsed: dict[str, Any] = {}
    for name in COLLECTOR_REQUIRED_FILES:
        try:
            parsed[name] = json.loads(files[name].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return ProbeResult(
                False, [f"{name} is not valid UTF-8 JSON ({type(error).__name__})"], []
            )

    run = parsed["run.json"]
    if not isinstance(run, dict):
        return ProbeResult(False, ["run.json must be a JSON object"], [])
    if run.get("contract_version") != COLLECTOR_RUN_CONTRACT_VERSION:
        return ProbeResult(
            False,
            [
                "run.json contract_version is "
                f"{run.get('contract_version')!r}, expected {COLLECTOR_RUN_CONTRACT_VERSION!r}"
            ],
            [],
        )
    missing_run_keys = [
        key for key in ("id", "input", "game_id", "status", "stop_reason") if key not in run
    ]
    if missing_run_keys:
        return ProbeResult(
            False, ["run.json is missing " + ", ".join(missing_run_keys)], []
        )
    if not _is_text(run["id"]) or not _is_text(run["game_id"]):
        return ProbeResult(False, ["run.json id and game_id must be non-empty strings"], [])
    if not isinstance(run["input"], dict):
        return ProbeResult(False, ["run.json input must be a JSON object"], [])
    if not isinstance(run["status"], str):
        return ProbeResult(False, ["run.json status must be a string"], [])
    if not isinstance(run["stop_reason"], (str, type(None))):
        return ProbeResult(False, ["run.json stop_reason must be a string or null"], [])
    if "interrupted" in run and not isinstance(run["interrupted"], bool):
        return ProbeResult(False, ["run.json interrupted must be a boolean"], [])

    limitations: list[str] = []
    inconsistencies: list[str] = []

    if run.get("interrupted") is True:
        limitations.append(
            "the original run is interrupted"
            f" (status={run['status']!r}, stop_reason={run.get('stop_reason')!r});"
            " its execution facts are kept as recorded"
        )
    if "finished_at" not in run or run["finished_at"] in (None, ""):
        limitations.append(
            "run.finished_at is missing; only the frozen bytes establish the checkpoint,"
            " not the run metadata"
        )
    if run.get("stop_reason") is None:
        limitations.append("run.stop_reason is null; the run states no stopping reason")

    sources = parsed["sources.json"]
    if not isinstance(sources, list):
        return ProbeResult(False, ["sources.json must be a JSON array"], [])
    sources_by_id: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            return ProbeResult(False, [f"sources.json [{index}] must be a JSON object"], [])
        if not _is_text(source.get("id")) or not _is_text(source.get("revision_id")):
            return ProbeResult(
                False,
                [f"sources.json [{index}] must have non-empty id and revision_id"],
                [],
            )
        if source["id"] in sources_by_id:
            inconsistencies.append(f"sources.json repeats source id {source['id']!r}")
        sources_by_id[source["id"]] = source

    records = parsed["records.json"]
    if not isinstance(records, list):
        return ProbeResult(False, ["records.json must be a JSON array"], [])
    seen_record_keys: set[tuple[str, str, str]] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            return ProbeResult(False, [f"records.json [{index}] must be a JSON object"], [])
        required = (
            "local_id",
            "record_type",
            "record_status",
            "reference_scope",
            "source_id",
            "revision_id",
        )
        missing = [key for key in required if not _is_text(record.get(key))]
        if missing:
            return ProbeResult(
                False,
                [f"records.json [{index}] has no non-empty {', '.join(missing)}"],
                [],
            )
        if record["record_type"] == "configurations" and "recommendation_status" not in record:
            return ProbeResult(
                False,
                [f"records.json [{index}] configuration has no recommendation_status"],
                [],
            )
        key = (record["reference_scope"], record["record_type"], record["local_id"])
        if key in seen_record_keys:
            inconsistencies.append(
                "records.json repeats "
                f"({record['reference_scope']!r}, {record['record_type']!r},"
                f" {record['local_id']!r})"
            )
        seen_record_keys.add(key)
        inconsistencies.extend(_record_reference_findings(record, index, sources_by_id, run))

    gaps = parsed["gaps.json"]
    if not isinstance(gaps, list):
        return ProbeResult(False, ["gaps.json must be a JSON array"], [])
    seen_gap_ids: set[str] = set()
    for index, gap in enumerate(gaps):
        if (
            not isinstance(gap, dict)
            or not _is_text(gap.get("id"))
            or not _is_text(gap.get("status"))
        ):
            return ProbeResult(
                False,
                [f"gaps.json [{index}] must have non-empty id and status"],
                [],
            )
        if gap["id"] in seen_gap_ids:
            inconsistencies.append(f"gaps.json repeats gap id {gap['id']!r}")
        seen_gap_ids.add(gap["id"])

    extractions = parsed["extractions.json"]
    if not isinstance(extractions, list):
        return ProbeResult(False, ["extractions.json must be a JSON array"], [])
    for index, extraction in enumerate(extractions):
        if not isinstance(extraction, dict):
            return ProbeResult(
                False, [f"extractions.json [{index}] must be a JSON object"], []
            )
        source_id = extraction.get("source_id")
        if not _is_text(source_id):
            inconsistencies.append(
                f"extractions.json [{index}] has no source_id to locate"
            )
        elif source_id not in sources_by_id:
            inconsistencies.append(
                f"extractions.json [{index}] refers to unknown source {source_id!r}"
            )

    candidate_limitations, candidate_inconsistencies = _candidate_findings(files, records)
    limitations.extend(candidate_limitations)
    inconsistencies.extend(candidate_inconsistencies)
    return ProbeResult(True, limitations, inconsistencies)


def _record_reference_findings(
    record: Mapping[str, Any],
    index: int,
    sources_by_id: Mapping[str, Mapping[str, Any]],
    run: Mapping[str, Any],
) -> list[str]:
    findings: list[str] = []
    scope = record["reference_scope"]
    if scope not in sources_by_id:
        findings.append(
            f"records.json [{index}] reference_scope {scope!r} does not locate a source"
        )
    source_id = record["source_id"]
    source = sources_by_id.get(source_id)
    if source is None:
        findings.append(
            f"records.json [{index}] refers to unknown source {source_id!r}"
        )
    elif record["revision_id"] != source["revision_id"]:
        findings.append(
            f"records.json [{index}] revision {record['revision_id']!r}"
            f" does not match source {source_id!r} revision {source['revision_id']!r}"
        )
    if record.get("game_id") is not None and record["game_id"] != run["game_id"]:
        findings.append(
            f"records.json [{index}] game_id {record['game_id']!r}"
            f" differs from run game_id {run['game_id']!r}"
        )
    return findings


def _candidate_findings(
    files: Mapping[str, bytes],
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    """Reconcile the derived candidate list with the exact rebuild.

    The rule is copied verbatim from ``game_collector/storage.py``; the on-disk
    list may omit derived entries but must never name a record the rebuild drops.
    A missing file is only a limitation, never a failure.
    """

    if "candidates.json" not in files:
        return ["candidates.json is absent; the derived candidate list was not reconciled"], []

    inconsistencies: list[str] = []
    try:
        on_disk = json.loads(files["candidates.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], ["candidates.json is not valid UTF-8 JSON"]
    if not isinstance(on_disk, list):
        return [], ["candidates.json must be a JSON array"]

    disk_ids: list[str] = []
    for index, row in enumerate(on_disk):
        if not isinstance(row, dict) or not _is_text(row.get("local_id")):
            inconsistencies.append(f"candidates.json [{index}] has no local_id")
            continue
        disk_ids.append(row["local_id"])
    if len(set(disk_ids)) != len(disk_ids):
        inconsistencies.append("candidates.json repeats a local_id")

    rebuilt = [
        row for row in records
        if row["record_type"] == "configurations"
        and row["record_status"] == "accepted"
        and row["recommendation_status"] != "excluded_branch"
        and not row.get("superseded_by")
    ]
    rebuilt_ids = {row["local_id"] for row in rebuilt}
    unknown = sorted(set(disk_ids) - rebuilt_ids)
    if unknown:
        inconsistencies.append(
            "candidates.json names local_id(s) that records.json does not derive: "
            + ", ".join(unknown)
        )
    return [], inconsistencies


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


# --- capture-scoped mapping (IMP-T02) ----------------------------------------

IMPORT_RULE_VERSION = "collector-import/1"

# Mirrors ``game_collector.schema.RECORD_GROUPS``; a test asserts they agree so
# the product never drifts from the collector's own type list.
COLLECTOR_RECORD_GROUPS = (
    "settings",
    "configurations",
    "environments",
    "evidence",
    "comparisons",
    "constraints",
    "version_impacts",
)

_IMPORT_ID_NAMESPACE = UUID("8a4b7c31-2d5e-4f6a-9b0c-1e2f3a4b5c6d")

# Payload paths that name another typed record in the same capture scope.
_SINGLE_REFERENCES = {
    "evidence": (("configuration_id", "configurations"), ("environment_id", "environments")),
    "constraints": (("setting_id", "settings"), ("evidence_id", "evidence")),
    "version_impacts": (("setting_id", "settings"), ("evidence_id", "evidence")),
}
_PLURAL_REFERENCES = {
    "evidence": (("setting_ids", "settings"), ("input_ids", "evidence")),
    "configurations": (("environment_ids", "environments"),),
    "comparisons": (("measurement_ids", "evidence"),),
}


@dataclass(frozen=True)
class ScopedImport:
    """Staging result of IMP-T02; IMP-T03 publishes it, nothing here is written."""

    import_snapshot_id: str
    legacy_run_id: str | None
    game_id: str | None
    sources: list[Source]
    source_revisions: list[SourceRevision]
    records: list[ImportedRecord]
    extractions: list[ImportedExtraction]
    validation: ImportValidation
    limitations: list[str]
    diagnostic_envelopes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def usable_records(self) -> list[ImportedRecord]:
        accepted = set(self.validation.accepted_record_ids)
        return [record for record in self.records if record.id in accepted]


def _import_id(import_snapshot_id: str, *parts: str) -> str:
    """Stable id for one (snapshot, role, legacy key, occurrence) tuple.

    A same-input retry must reproduce every id it wrote into its declaration
    manifest, otherwise a crash between publishing and committing would leave
    two different snapshots of the same import.
    """
    return uuid5(_IMPORT_ID_NAMESPACE, ":".join((import_snapshot_id, *parts))).hex


def canonical_source_url(url: str) -> str:
    """Normalise only what is unambiguous; query parameters are all kept."""
    split = urlsplit(url)
    scheme = split.scheme.lower()
    host = (split.hostname or "").lower()
    if split.port is not None and not (
        (scheme == "http" and split.port == 80) or (scheme == "https" and split.port == 443)
    ):
        host = f"{host}:{split.port}"
    if not host:
        return url
    path = split.path
    query = f"?{split.query}" if split.query else ""
    return f"{scheme}://{host}{path}{query}"


def read_frozen_bytes(frozen: FrozenGroup, files: FileService) -> dict[str, bytes]:
    """Read every member of the frozen group through managed-file reads only."""
    missing = [name for name in COLLECTOR_REQUIRED_FILES if name not in frozen.file_ids]
    if missing:
        raise ContractError(
            ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
            "the frozen group is missing " + ", ".join(missing),
        )
    return {
        name: files.read_managed_file(file_id)
        for name, file_id in frozen.file_ids.items()
    }


def _parse_member(members: Mapping[str, bytes], name: str) -> Any:
    try:
        return json.loads(members[name].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(
            ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
            f"{name} is not valid UTF-8 JSON; the group cannot be mapped",
        ) from error


def _check(
    check_id: str,
    status: CheckStatus,
    note: str,
    *,
    method: str = "deterministic import rule",
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        definition_version=IMPORT_RULE_VERSION,
        status=status,
        input_refs=[],
        evidence_refs=[],
        checked_at=None if status is CheckStatus.not_run else utc_now(),
        method=method,
        note=note,
    )


class ScopedReferenceMapper:
    """Allocate typed nodes, then resolve payload references by four-part key.

    Allocation happens for *every* record before any reference is resolved, so
    row order cannot decide whether a forward reference is found.  A record is
    isolated when its own scope/source cannot be located or when anything it
    references is isolated; independent records survive.
    """

    def __init__(self, files: FileService, *, clock: Callable[[], str] = utc_now) -> None:
        self.files = files
        self.clock = clock

    def map(
        self,
        frozen: FrozenGroup,
        *,
        import_snapshot_id: str,
        identity_mappings: Sequence[IdentityMapping] = (),
    ) -> ScopedImport:
        members = read_frozen_bytes(frozen, self.files)
        run = _parse_member(members, "run.json")
        sources = _parse_member(members, "sources.json")
        records = _parse_member(members, "records.json")
        extractions = _parse_member(members, "extractions.json")
        if not isinstance(run, dict) or not isinstance(sources, list) \
                or not isinstance(records, list) or not isinstance(extractions, list):
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                "the frozen group's top-level shapes no longer form a collector run",
            )

        limitations: list[str] = []
        game_id, game_limits = _resolve_game(run, identity_mappings, self.files)
        limitations.extend(game_limits)

        mapped_sources, mapped_revisions, revision_by_source = _map_sources(
            sources, import_snapshot_id, self.clock
        )
        limitations.extend(_source_limitations(mapped_revisions))

        nodes, envelopes, register_limits = _allocate_nodes(
            records, import_snapshot_id, revision_by_source, self.clock
        )
        limitations.extend(register_limits)
        imported_records, diagnostic_ids, resolve_limits = _resolve_nodes(
            nodes,
            import_snapshot_id=import_snapshot_id,
            revision_by_source=revision_by_source,
            clock=self.clock,
        )
        limitations.extend(resolve_limits)

        accepted_ids = [
            record.id for record in imported_records if record.id not in diagnostic_ids
        ]
        validation = ImportValidation(
            accepted_record_ids=accepted_ids,
            diagnostic_record_ids=[
                record.id for record in imported_records if record.id in diagnostic_ids
            ],
            checks=[_check(
                "import.records",
                CheckStatus.pass_,
                f"{len(imported_records)} record(s) were allocated in "
                f"{len({record.capture_scope_id for record in imported_records})}"
                " capture scope(s)",
            )],
        )
        mapped_extractions, extraction_limits = _map_extractions(
            extractions, import_snapshot_id, revision_by_source, self.clock
        )
        limitations.extend(extraction_limits)
        return ScopedImport(
            import_snapshot_id=import_snapshot_id,
            legacy_run_id=run.get("id"),
            game_id=game_id,
            sources=mapped_sources,
            source_revisions=mapped_revisions,
            records=imported_records,
            extractions=mapped_extractions,
            validation=validation,
            limitations=limitations,
            diagnostic_envelopes=envelopes,
        )


class ImportValidator:
    """The staging validator: same allocation rules, only the local result."""

    def __init__(self, mapper: ScopedReferenceMapper) -> None:
        self.mapper = mapper

    def validate(
        self,
        frozen: FrozenGroup,
        *,
        import_snapshot_id: str,
        identity_mappings: Sequence[IdentityMapping] = (),
    ) -> ImportValidation:
        return self.mapper.map(
            frozen,
            import_snapshot_id=import_snapshot_id,
            identity_mappings=identity_mappings,
        ).validation


# --- source and identity mapping ---------------------------------------------


def _map_sources(
    rows: Sequence[Any],
    import_snapshot_id: str,
    clock: Callable[[], str],
) -> tuple[list[Source], list[SourceRevision], dict[tuple[str, str], str]]:
    sources: list[Source] = []
    revisions: list[SourceRevision] = []
    revision_by_source: dict[tuple[str, str], str] = {}
    for row in rows:
        source_id = row["id"]
        revision_id = row["revision_id"]
        document = row.get("document")
        if document is not None and not isinstance(document, dict):
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                f"source {source_id!r} has a document that is not an object",
            )
        content_hash = None
        truncated = False
        capture_method = "collector_run_metadata"
        document_note = (
            "the frozen group carries no source document; only its metadata was captured"
        )
        if isinstance(document, dict):
            content_hash = document.get("content_hash")
            truncated = bool(document.get("truncated", False))
            retrieval_method = document.get("retrieval_method")
            if isinstance(retrieval_method, str) and retrieval_method.strip():
                capture_method = retrieval_method
            # The inline document is already preserved verbatim inside the
            # frozen sources.json original.  Registering a second ManagedFile
            # would only leave an ownerless file when the import is rejected.
            document_note = (
                "the inline document exists only inside the frozen sources.json"
                " original; it is not registered as a separate managed file"
            )
        source = Source(
            id=_import_id(import_snapshot_id, "source", source_id),
            schema_version="product-contract/0.4",
            created_at=clock(),
            created_by_task_id=None,
            original_url=row["url"],
            canonical_url=canonical_source_url(row["url"]),
            identity_checks=[_check(
                "source.identity",
                CheckStatus.not_run,
                "an identical URL does not prove the same author or an independent test",
            )],
        )
        revision = SourceRevision(
            id=_import_id(import_snapshot_id, "source_revision", source_id, revision_id),
            schema_version="product-contract/0.4",
            created_at=clock(),
            created_by_task_id=None,
            source_id=source.id,
            retrieved_at=row["retrieved_at"],
            upstream_revision_id=revision_id,
            content_hash=content_hash,
            document_file_id=None,
            capture_method=capture_method,
            capture_scope=source_id,
            metadata=SourceMetadata(
                author=None,
                published_date=None,
                updated_date=None,
                test_date=None,
                original_source_url=None,
            ),
            truncated=truncated,
            checks=[_check("source.document", CheckStatus.not_run, document_note)],
        )
        sources.append(source)
        revisions.append(revision)
        revision_by_source[(source_id, revision_id)] = revision.id
    return sources, revisions, revision_by_source


def _source_limitations(revisions: Sequence[SourceRevision]) -> list[str]:
    return [
        f"source revision {revision.capture_scope!r} has no separately managed document;"
        " its content is not addressable as a ManagedFile"
        for revision in revisions
        if revision.document_file_id is None
    ]


def _resolve_game(
    run: Mapping[str, Any],
    identity_mappings: Sequence[IdentityMapping],
    files: FileService,
) -> tuple[str | None, list[str]]:
    """A legacy game id becomes a public game id only through its own mapping."""
    legacy_value = run.get("game_id")
    namespace = f"collector_run:{run.get('id')}"
    candidates = [
        mapping for mapping in identity_mappings
        if mapping.legacy_value == legacy_value and mapping.legacy_namespace == namespace
    ]
    targets = {mapping.target_game_id for mapping in candidates if mapping.target_game_id}
    if len(targets) > 1:
        return None, [
            f"the legacy game id {legacy_value!r} has conflicting identity mappings;"
            " no public game id was assumed"
        ]
    if not targets:
        return None, [
            f"the legacy game id {legacy_value!r} has no identity mapping in {namespace!r};"
            " no public game id was assumed"
        ]
    target = targets.pop()
    # A mapping names the stable game series, not one version object.
    with files.repository.unit_of_work() as uow:
        latest = uow.latest_for_series("Game", target)
    if latest is None:
        return None, [
            f"the identity mapping for {legacy_value!r} targets a game series that does not exist;"
            " no public game id was assumed"
        ]
    return target, []


# --- record allocation and resolution ----------------------------------------


@dataclass
class _Node:
    row: dict[str, Any]
    record_id: str
    key: tuple[str, str, str]
    payload: Any
    reasons: list[str]
    quarantined: bool = False
    diagnostic: bool = False
    record: ImportedRecord | None = None


def _allocate_nodes(
    rows: Sequence[Any],
    import_snapshot_id: str,
    revision_by_source: Mapping[tuple[str, str], str],
    clock: Callable[[], str],
) -> tuple[list[_Node], list[dict[str, Any]], list[str]]:
    """Allocate one id per row before any reference is resolved."""
    counts: dict[tuple[str, str, str], int] = {}
    for row in rows:
        key = (row["reference_scope"], row["record_type"], row["local_id"])
        counts[key] = counts.get(key, 0) + 1

    nodes: list[_Node] = []
    envelopes: list[dict[str, Any]] = []
    limitations: list[str] = []
    seen: dict[tuple[str, str, str], int] = {}
    for row in rows:
        key = (row["reference_scope"], row["record_type"], row["local_id"])
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        record_id = _import_id(
            import_snapshot_id, "record", key[0], key[1], key[2], str(occurrence)
        )
        reasons: list[str] = []
        if counts[key] > 1:
            reasons.append(f"duplicate key {key!r} in one group")
        if row["record_type"] not in COLLECTOR_RECORD_GROUPS:
            reasons.append(f"unregistered record type {row['record_type']!r}")
        try:
            LegacyRecordState(row["record_status"])
        except ValueError:
            limitations.append(
                f"record {row['id']!r} states an unknown status {row['record_status']!r};"
                " its envelope is kept as a diagnostic original"
            )
            envelopes.append(copy.deepcopy(dict(row)))
            continue
        if (row["source_id"], row["revision_id"]) not in revision_by_source:
            limitations.append(
                f"record {row['id']!r} does not locate a frozen source revision;"
                " its envelope is kept as a diagnostic original"
            )
            envelopes.append(copy.deepcopy(dict(row)))
            continue
        node = _Node(
            row=copy.deepcopy(dict(row)),
            record_id=record_id,
            key=key,
            payload=copy.deepcopy(row.get("payload")),
            reasons=reasons,
            quarantined=row["record_status"] == LegacyRecordState.quarantined.value,
        )
        nodes.append(node)
    return nodes, envelopes, limitations


def record_payload_references(
    record_type: str, payload: Any
) -> list[tuple[str, str, str]]:
    """Public view of the typed references one payload states (02a §4.1).

    ``(target record type, local id, payload path)`` — the same relation the
    importer resolves, exposed so a package builder can re-check closure
    without a second, drifting copy of the reference table.
    """
    return _payload_references(record_type, payload)


def _payload_references(record_type: str, payload: Any) -> list[tuple[str, str, str]]:
    """(target type, local id, payload path) for one record's typed references."""
    if not isinstance(payload, dict):
        return []
    references: list[tuple[str, str, str]] = []
    for field_name, target in _SINGLE_REFERENCES.get(record_type, ()):
        value = payload.get(field_name)
        if isinstance(value, str) and value:
            references.append((target, value, field_name))
    for field_name, target in _PLURAL_REFERENCES.get(record_type, ()):
        values = payload.get(field_name)
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values):
            if isinstance(value, str) and value:
                references.append((target, value, f"{field_name}.{index}"))
    if record_type == "configurations":
        assignments = payload.get("assignments")
        if isinstance(assignments, list):
            for index, assignment in enumerate(assignments):
                if not isinstance(assignment, dict):
                    continue
                setting_id = assignment.get("setting_id")
                if isinstance(setting_id, str) and setting_id:
                    references.append(("settings", setting_id, f"assignments.{index}.setting_id"))
                evidence_ids = assignment.get("evidence_ids")
                if isinstance(evidence_ids, list):
                    for inner, evidence_id in enumerate(evidence_ids):
                        if isinstance(evidence_id, str) and evidence_id:
                            references.append((
                                "evidence", evidence_id,
                                f"assignments.{index}.evidence_ids.{inner}",
                            ))
    return references


def _resolve_nodes(
    nodes: Sequence[_Node],
    *,
    import_snapshot_id: str,
    revision_by_source: Mapping[tuple[str, str], str],
    clock: Callable[[], str],
) -> tuple[list[ImportedRecord], set[str], list[str]]:
    by_scope: dict[tuple[str, str, str], list[_Node]] = {}
    legacy_index: dict[tuple[str, str], list[_Node]] = {}
    for node in nodes:
        by_scope.setdefault(node.key, []).append(node)
        legacy_index.setdefault(
            (node.key[0], node.row["id"]), []
        ).append(node)
    for group in by_scope.values():
        if len(group) > 1:
            for node in group:
                node.diagnostic = True

    unresolved: dict[str, list[str]] = {}
    superseded: dict[str, tuple[CheckStatus, str]] = {}
    for node in nodes:
        missing: list[str] = []
        for target_type, local_id, path in _payload_references(
            node.key[1], node.payload
        ):
            target_key = (node.key[0], target_type, local_id)
            found = by_scope.get(target_key, [])
            if len(found) != 1:
                missing.append(f"{path} -> {target_type} {local_id!r}")
        unresolved[node.record_id] = missing

        superseded_by = node.row.get("superseded_by")
        if not superseded_by:
            superseded[node.record_id] = (CheckStatus.pass_, "the record names no successor")
        else:
            candidates = [
                candidate for candidate in legacy_index.get((node.key[0], superseded_by), [])
                if candidate.row["source_id"] == node.row["source_id"]
                and candidate.row["revision_id"] == node.row["revision_id"]
                and candidate.key[1] == node.key[1]
            ]
            if len(candidates) == 1:
                superseded[node.record_id] = (
                    CheckStatus.pass_,
                    f"{superseded_by!r} resolves to {candidates[0].record_id}",
                )
            else:
                superseded[node.record_id] = (
                    CheckStatus.unknown,
                    f"{superseded_by!r} was not found in this capture scope and source"
                    " revision; no other run's record was substituted",
                )

    for node in nodes:
        if node.reasons or node.quarantined:
            node.diagnostic = True

    # Isolate dependents of an isolated record, to a fixpoint.
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.diagnostic:
                continue
            for target_type, local_id, _path in _payload_references(
                node.key[1], node.payload
            ):
                found = by_scope.get((node.key[0], target_type, local_id), [])
                if len(found) == 1 and found[0].diagnostic:
                    node.diagnostic = True
                    changed = True
                    break

    limitations: list[str] = []
    records: list[ImportedRecord] = []
    diagnostic_ids: set[str] = set()
    for node in nodes:
        missing = unresolved[node.record_id]
        if missing:
            node.diagnostic = True
            limitations.append(
                f"record {node.row['id']!r} references records it cannot locate: "
                + ", ".join(missing)
            )
        if node.diagnostic:
            diagnostic_ids.add(node.record_id)

        reference_status = CheckStatus.fail if missing else CheckStatus.pass_
        reference_note = (
            "unresolved: " + ", ".join(missing) if missing
            else "every typed reference resolves in this capture scope"
        )
        checks = [
            _check(
                "import.key",
                CheckStatus.fail if node.reasons else CheckStatus.pass_,
                "; ".join(node.reasons) if node.reasons
                else "the four-part key occurs once in this group",
            ),
            _check("import.reference", reference_status, reference_note),
            _check(
                "import.superseded_by",
                superseded[node.record_id][0],
                superseded[node.record_id][1],
            ),
            _check(
                "import.upstream_status",
                CheckStatus.not_run,
                "the import neither raises nor lowers the original record status",
            ),
            _check(
                "import.applicability",
                CheckStatus.not_run,
                "the original applicability was not re-evaluated",
            ),
            _check(
                "import.identity",
                CheckStatus.not_run,
                "no public setting identity mapping was applied",
            ),
        ]
        if node.quarantined:
            checks.append(_check(
                "import.usable",
                CheckStatus.fail,
                "the original record is quarantined; copying it does not upgrade it",
            ))
        if node.row["record_type"] in ("settings", "configurations") and node.row.get(
            "recommendation_status"
        ):
            checks.append(_check(
                "import.recommendation_status",
                CheckStatus.not_run,
                "the legacy recommendation status is preserved, not re-judged",
            ))
        record = ImportedRecord(
            id=node.record_id,
            schema_version="product-contract/0.4",
            created_at=clock(),
            created_by_task_id=None,
            record_snapshot_id=node.record_id,
            import_snapshot_id=import_snapshot_id,
            capture_scope_id=node.key[0],
            local_record_id=node.key[2],
            legacy_record_id=node.row["id"],
            source_revision_id=revision_by_source[
                (node.row["source_id"], node.row["revision_id"])
            ],
            record_type=node.key[1],
            payload=node.payload,
            payload_hash=hashlib.sha256(canonical_json(node.payload)).hexdigest(),
            upstream_status=LegacyRecordState(node.row["record_status"]),
            upstream_envelope=node.row,
            checks=checks,
            extension=_extension(node.key[1], node.payload),
        )
        node.record = record
        records.append(record)
    return records, diagnostic_ids, limitations


def _extension(record_type: str, payload: Any) -> RecordExtension:
    boundary = None
    extra = [_check(
        "import.identity",
        CheckStatus.not_run,
        "no public setting identity mapping was applied",
    )]
    if record_type == "evidence" and isinstance(payload, dict):
        numeric = payload.get("numeric")
        if isinstance(numeric, dict) and numeric.get("number_kind") in (
            "interval", "lower_bound", "upper_bound",
        ):
            boundary = BoundarySemantics(
                lower_inclusive=None,
                upper_inclusive=None,
                evidence_refs=[],
                status=CheckStatus.unknown,
            )
            extra.append(_check(
                "import.boundary_semantics",
                CheckStatus.unknown,
                "the legacy numeric value states no inclusive/exclusive flag;"
                " strictness stays unknown rather than defaulting to >=",
            ))
    return RecordExtension(
        boundary_semantics=boundary,
        setting_identity_ref=None,
        checks=extra,
    )


def _map_extractions(
    rows: Sequence[Any],
    import_snapshot_id: str,
    revision_by_source: Mapping[tuple[str, str], str],
    clock: Callable[[], str],
) -> tuple[list[ImportedExtraction], list[str]]:
    extractions: list[ImportedExtraction] = []
    limitations: list[str] = []
    for index, row in enumerate(rows):
        legacy_source = row.get("source_id")
        revision_id = next(
            (
                value for (source_id, _revision), value in revision_by_source.items()
                if source_id == legacy_source
            ),
            None,
        )
        note = (
            "the legacy extraction names a requested model but does not prove the"
            " actual returned model; returned_model is null"
        )
        limitations.append(f"extraction[{index}]: {note}")
        extractions.append(ImportedExtraction(
            id=_import_id(import_snapshot_id, "extraction", str(index)),
            schema_version="product-contract/0.4",
            created_at=clock(),
            created_by_task_id=None,
            import_snapshot_id=import_snapshot_id,
            source_revision_id=revision_id,
            legacy_source_id=legacy_source or "",
            status=row.get("status"),
            model_requested=row.get("model"),
            returned_model=None,
            upstream_envelope=copy.deepcopy(dict(row)),
            checks=[_check(
                "import.model_returned",
                CheckStatus.unknown,
                note,
            )],
            limitations=[note],
        ))
    return extractions, limitations



# --- publication and recovery (IMP-T03) --------------------------------------

IMPORT_PUBLISH_ROUTE = "/internal/knowledge-import-publish"
_SNAPSHOT_ID_NAMESPACE = UUID("d3f1a2b4-5c6d-4e7f-8a90-1b2c3d4e5f60")
DECLARATION_FILE = "declaration.json"


def import_fingerprint(manifest: FileManifest) -> str:
    """Canonical name + byte length + sha256 over the frozen group.

    The original bytes decide the identity; a re-serialised JSON view does not.
    """
    return hashlib.sha256(canonical_json([
        {
            "relative_name": entry.relative_name,
            "byte_length": entry.byte_length,
            "sha256": entry.sha256,
        }
        for entry in sorted(manifest.entries, key=lambda item: item.relative_name)
    ])).hexdigest()


def mapping_context_hash(mapping_version: str, identity_mapping_ids: Sequence[str]) -> str:
    """What the mapping interpreted, not what the bytes were."""
    return hashlib.sha256(canonical_json({
        "mapping_version": mapping_version,
        "identity_mapping_ids": sorted(set(identity_mapping_ids)),
        "rule_version": IMPORT_RULE_VERSION,
    })).hexdigest()


def _snapshot_id(fingerprint: str, mapping_version: str, context_hash: str) -> str:
    return uuid5(
        _SNAPSHOT_ID_NAMESPACE,
        ":".join(("import_snapshot", fingerprint, mapping_version, context_hash)),
    ).hex


@dataclass(frozen=True)
class ImportPublishResult:
    snapshot_id: str
    status: PublicationStatus
    code: str
    import_fingerprint: str
    mapping_context_hash: str
    parent_import_snapshot_id: str | None
    game_id: str | None
    accepted_record_ids: list[str]
    diagnostic_record_ids: list[str]
    declaration_file_id: str
    limitations: list[str]
    refs: list[ObjectRef]


def snapshot_manifest(manifest: FileManifest) -> SnapshotManifest:
    return SnapshotManifest(entries=[
        SnapshotFileEntry(
            relative_name=entry.relative_name,
            byte_length=entry.byte_length,
            sha256=entry.sha256,
            media_type=entry.media_type,
        )
        for entry in sorted(manifest.entries, key=lambda item: item.relative_name)
    ])


def _validation_message(error: ValidationError) -> str:
    """One stable sentence; the raw pydantic text never reaches the user."""
    locations = sorted({
        ".".join(str(part) for part in item["loc"]) for item in error.errors()
    })
    return (
        "the frozen group does not satisfy the product contract: "
        + ", ".join(locations)
    )


def frozen_group_from_file_ids(
    files: FileService,
    file_ids: Sequence[str],
    registry: FormatRegistry | None = None,
) -> FrozenGroup:
    """Re-open a frozen group from the managed ids a task input names.

    A client never supplies a directory; the bytes and their names come from
    the managed-file records themselves.
    """
    registry = registry or default_format_registry()
    if not file_ids:
        raise ContractError(
            ErrorCode.INPUT_VALIDATION_FAILED, "a knowledge import needs at least one file id"
        )
    entries: list[FileEntry] = []
    by_name: dict[str, str] = {}
    for file_id in file_ids:
        stored = files.repository.get_object("ManagedFile", file_id)
        entry = FileEntry.model_validate(stored.payload["manifest"])
        if entry.relative_name in by_name:
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                f"two managed files are named {entry.relative_name!r}",
            )
        by_name[entry.relative_name] = file_id
        entries.append(entry)
    data = {
        name: files.read_managed_file(file_id) for name, file_id in by_name.items()
    }
    inspection, inconsistencies = registry._probe(data)
    if inspection.format_id is None:
        if inspection.missing_files:
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                "the file group is missing " + ", ".join(inspection.missing_files),
            )
        raise ContractError(
            ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
            "no registered format matches this file group",
        )
    if inconsistencies:
        # freeze() refuses such a group too; the managed-id entry path must not
        # be a weaker door into the same importer.
        raise ContractError(
            ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
            "; ".join(inconsistencies),
        )
    return FrozenGroup(
        format_id=inspection.format_id,
        operation_id="",
        file_ids=by_name,
        manifest=FileManifest(
            entries=sorted(entries, key=lambda item: item.relative_name)
        ),
        inspection=inspection,
    )


class ImportPublisher:
    """Publish one frozen group as an ImportSnapshot, idempotently.

    ``FileService.publish`` owns the file window: a rename without a commit
    leaves an unconsumable orphan that a same-key retry reuses, and a lost
    response after the commit replays the original response.
    """

    def __init__(
        self,
        repository: ProductRepository,
        files: FileService,
        *,
        registry: FormatRegistry | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = repository
        self.files = files
        self.registry = registry or default_format_registry()
        self.clock = clock

    # -- public entry -------------------------------------------------------
    def publish(
        self,
        *,
        frozen: FrozenGroup,
        game_context_id: str,
        mapping_version: str,
        identity_mapping_ids: Sequence[str],
        user_scope_id: str,
        idempotency_key: str | None = None,
        canonical_route: str = IMPORT_PUBLISH_ROUTE,
        hooks: PublishHooks | None = None,
        created_by_task_id: str | None = None,
    ) -> ImportPublishResult:
        fingerprint = import_fingerprint(frozen.manifest)
        context_hash = mapping_context_hash(mapping_version, identity_mapping_ids)
        snapshot_id = _snapshot_id(fingerprint, mapping_version, context_hash)

        # Ownership is checked before the dedupe replay: an already published
        # snapshot must not become a door into another player's declaration.
        mappings = [
            self._identity_mapping(mapping_id, user_scope_id)
            for mapping_id in identity_mapping_ids
        ]

        existing = self._find_by_triple(fingerprint, mapping_version, context_hash)
        if existing is not None:
            # The identity is already registered: a retry returns the original
            # import, including a rejected one.  It never re-runs the mapping,
            # and it does not re-validate references that have since changed.
            return self._result(existing["import_snapshot_id"])

        self._require_game_context(game_context_id, user_scope_id)

        mapped: ScopedImport | None = None
        rejection: str | None = None
        try:
            mapped = ScopedReferenceMapper(self.files, clock=self.clock).map(
                frozen,
                import_snapshot_id=snapshot_id,
                identity_mappings=mappings,
            )
        except ContractError as error:
            if error.code is not ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT:
                raise
            rejection = error.message
        except ValidationError as error:
            # The bytes are recognised as a collector run but do not satisfy the
            # product contract.  That is a rejected import, never an internal
            # error: the originals are kept and the reason is recorded.
            rejection = _validation_message(error)

        status = (
            PublicationStatus.rejected if rejection is not None else PublicationStatus.published
        )
        limitations = list(mapped.limitations) if mapped else [rejection or "the group was rejected"]
        # What the format probe could not prove travels with the snapshot: an
        # interrupted original run, a missing finished_at or an unreconciled
        # candidate list must stay visible downstream.
        if mapped is not None:
            limitations.extend(
                f"format: {item}" for item in frozen.inspection.limitations
            )
        if rejection is not None:
            limitations.insert(0, f"the frozen group was rejected: {rejection}")
        game_id = mapped.game_id if mapped else None
        accepted_ids = list(mapped.validation.accepted_record_ids) if mapped else []
        diagnostic_ids = list(mapped.validation.diagnostic_record_ids) if mapped else []
        declaration = {
            "kind": "collector_import",
            "rule_version": IMPORT_RULE_VERSION,
            "import_snapshot_id": snapshot_id,
            "import_fingerprint": fingerprint,
            "mapping_version": mapping_version,
            "mapping_context_hash": context_hash,
            "identity_mapping_ids": sorted(set(identity_mapping_ids)),
            "game_context_id": game_context_id,
            "origin": "collector_run",
            "legacy_run_id": mapped.legacy_run_id if mapped else self._legacy_run_id(frozen),
            "import_status": status.value,
            "rejection_reason": rejection,
            "accepted_record_ids": accepted_ids,
            "diagnostic_record_ids": diagnostic_ids,
            "limitations": limitations,
            "manifest": frozen.manifest.model_dump(mode="json"),
            # The published snapshot must locate the exact originals it was
            # built from; a name/hash list alone cannot be re-read.
            "frozen_file_ids": dict(frozen.file_ids),
        }
        key = idempotency_key or f"snapshot:{snapshot_id}"
        request_hash = hashlib.sha256(canonical_json({
            "import_fingerprint": fingerprint,
            "mapping_version": mapping_version,
            "mapping_context_hash": context_hash,
            "user_scope_id": user_scope_id,
        })).hexdigest()
        self.files.publish(
            kind="import",
            files={f"imports/{snapshot_id}/{DECLARATION_FILE}": canonical_json(declaration)},
            user_scope_id=user_scope_id,
            method="POST",
            canonical_route=canonical_route,
            key=key,
            request_schema_version="product-contract/0.4",
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write(
                uow,
                managed=managed,
                snapshot_id=snapshot_id,
                fingerprint=fingerprint,
                mapping_version=mapping_version,
                context_hash=context_hash,
                identity_mapping_ids=list(identity_mapping_ids),
                mapped=mapped,
                status=status,
                limitations=limitations,
                game_id=game_id,
                created_by_task_id=created_by_task_id,
                legacy_run_id=declaration["legacy_run_id"],
                group_manifest=frozen.manifest,
                frozen_file_ids=list(frozen.file_ids.values()),
            ),
            hooks=hooks,
        )
        return self._result(snapshot_id)

    # -- the publishing transaction ----------------------------------------
    def _write(
        self,
        uow: UnitOfWork,
        *,
        managed: Sequence[Any],
        snapshot_id: str,
        fingerprint: str,
        mapping_version: str,
        context_hash: str,
        identity_mapping_ids: Sequence[str],
        mapped: ScopedImport | None,
        status: PublicationStatus,
        limitations: Sequence[str],
        game_id: str | None,
        created_by_task_id: str | None,
        legacy_run_id: str | None,
        group_manifest: FileManifest,
        frozen_file_ids: Sequence[str],
    ) -> list[ObjectRef]:
        winner = uow.query(
            "SELECT import_snapshot_id FROM import_snapshots"
            " WHERE import_fingerprint=? AND mapping_version=? AND mapping_context_hash=?",
            (fingerprint, mapping_version, context_hash),
        )
        if winner:
            return self._refs(uow, winner[0]["import_snapshot_id"])

        parent = uow.query(
            "SELECT import_snapshot_id FROM import_snapshots WHERE import_fingerprint=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (fingerprint,),
        )
        parent_id = parent[0]["import_snapshot_id"] if parent else None
        declaration_file_id = managed[0].id
        snapshot = ImportSnapshot(
            id=snapshot_id,
            schema_version="product-contract/0.4",
            created_at=self.clock(),
            created_by_task_id=created_by_task_id,
            origin="collector_run",
            legacy_run_id=legacy_run_id,
            manifest=snapshot_manifest(group_manifest),
            import_fingerprint=fingerprint,
            import_status=status,
            mapping_version=mapping_version,
            mapping_context_hash=context_hash,
            parent_import_snapshot_id=parent_id,
            identity_mapping_ids=list(identity_mapping_ids),
            limitations=list(limitations),
        )
        uow.insert_object("ImportSnapshot", snapshot)
        if mapped is not None:
            for source in mapped.sources:
                uow.insert_object("Source", source)
            for revision in mapped.source_revisions:
                uow.insert_object("SourceRevision", revision)
            diagnostic = set(mapped.validation.diagnostic_record_ids)
            for record in mapped.records:
                uow.insert_object("ImportedRecord", record)
                uow.claim_imported_record_key(
                    record_snapshot_id=record.id,
                    import_snapshot_id=snapshot_id,
                    capture_scope_id=record.capture_scope_id,
                    record_type=record.record_type,
                    local_record_id=record.local_record_id,
                    usable=record.id not in diagnostic,
                )
            for extraction in mapped.extractions:
                uow.insert_object("ImportedExtraction", extraction)
            # The dependency graph is a real reference row per member.  The
            # declaration ManagedFile itself is inserted by FileService after
            # this writer, so its id lives in the projection table instead.
            snapshot_ref = ObjectRef(entity_type="ImportSnapshot", entity_id=snapshot_id)
            for source in mapped.sources:
                uow.insert_reference(snapshot_id, "source", ObjectRef(
                    entity_type="Source", entity_id=source.id,
                ))
            for revision in mapped.source_revisions:
                uow.insert_reference(snapshot_id, "source_revision", ObjectRef(
                    entity_type="SourceRevision", entity_id=revision.id,
                ))
            for record in mapped.records:
                uow.insert_reference(snapshot_id, "record", ObjectRef(
                    entity_type="ImportedRecord", entity_id=record.id,
                ))
                uow.insert_reference(record.id, "source_revision", ObjectRef(
                    entity_type="SourceRevision", entity_id=record.source_revision_id,
                ))
                uow.insert_reference(record.id, "import_snapshot", snapshot_ref)
            for extraction in mapped.extractions:
                uow.insert_reference(snapshot_id, "extraction", ObjectRef(
                    entity_type="ImportedExtraction", entity_id=extraction.id,
                ))
            for file_id in frozen_file_ids:
                uow.insert_reference(snapshot_id, "frozen_file", ObjectRef(
                    entity_type="ManagedFile", entity_id=file_id,
                ))
        uow.execute(
            "INSERT INTO import_snapshots(import_snapshot_id, import_fingerprint,"
            " mapping_version, mapping_context_hash, import_status, declaration_file_id,"
            " parent_import_snapshot_id, mapped_game_id, created_by_task_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                fingerprint,
                mapping_version,
                context_hash,
                status.value,
                declaration_file_id,
                parent_id,
                game_id,
                created_by_task_id,
                self.clock(),
            ),
        )
        # The snapshot is born in its final state: one creation event with no
        # previous state.  No staging row is invented for the audit log.
        related = [
            ObjectRef(entity_type="ManagedFile", entity_id=declaration_file_id),
            *(
                ObjectRef(entity_type="ManagedFile", entity_id=file_id)
                for file_id in frozen_file_ids
            ),
        ]
        uow.record_domain_event(
            target_type="ImportSnapshot",
            target_id=snapshot_id,
            dimension="publication",
            sequence=1,
            previous_state=None,
            next_state=status.value,
            cause=(
                "import_published"
                if status is PublicationStatus.published
                else "import_rejected"
            ),
            related_refs=related,
            created_at=self.clock(),
        )
        return self._refs(uow, snapshot_id)

    # -- result reconstruction ---------------------------------------------
    def _result(self, snapshot_id: str) -> ImportPublishResult:
        with self.repository.unit_of_work() as uow:
            row = uow.query(
                "SELECT * FROM import_snapshots WHERE import_snapshot_id=?", (snapshot_id,)
            )
            if not row:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the import snapshot is not registered"
                )
            keys = uow.query(
                "SELECT record_snapshot_id, usable FROM imported_record_keys"
                " WHERE import_snapshot_id=? ORDER BY rowid",
                (snapshot_id,),
            )
        stored = self.repository.get_object("ImportSnapshot", snapshot_id)
        snapshot = ImportSnapshot.model_validate_json(stored.payload_bytes)
        accepted = [item["record_snapshot_id"] for item in keys if item["usable"]]
        diagnostic = [item["record_snapshot_id"] for item in keys if not item["usable"]]
        code = "published" if accepted and row[0]["mapped_game_id"] else "retained_only"
        return ImportPublishResult(
            snapshot_id=snapshot_id,
            status=PublicationStatus(row[0]["import_status"]),
            code=code,
            import_fingerprint=row[0]["import_fingerprint"],
            mapping_context_hash=row[0]["mapping_context_hash"],
            parent_import_snapshot_id=row[0]["parent_import_snapshot_id"],
            game_id=row[0]["mapped_game_id"],
            accepted_record_ids=accepted,
            diagnostic_record_ids=diagnostic,
            declaration_file_id=row[0]["declaration_file_id"],
            limitations=list(snapshot.limitations),
            refs=[
                ObjectRef(entity_type="ImportSnapshot", entity_id=snapshot_id),
                ObjectRef(entity_type="ManagedFile", entity_id=row[0]["declaration_file_id"]),
            ],
        )

    def _refs(self, uow: UnitOfWork, snapshot_id: str) -> list[ObjectRef]:
        row = uow.query(
            "SELECT declaration_file_id FROM import_snapshots WHERE import_snapshot_id=?",
            (snapshot_id,),
        )
        refs = [ObjectRef(entity_type="ImportSnapshot", entity_id=snapshot_id)]
        if row:
            refs.append(ObjectRef(
                entity_type="ManagedFile", entity_id=row[0]["declaration_file_id"]
            ))
        return refs

    def _find_by_triple(
        self, fingerprint: str, mapping_version: str, context_hash: str
    ) -> Any | None:
        with self.repository.unit_of_work() as uow:
            rows = uow.query(
                "SELECT * FROM import_snapshots WHERE import_fingerprint=?"
                " AND mapping_version=? AND mapping_context_hash=?",
                (fingerprint, mapping_version, context_hash),
            )
        return rows[0] if rows else None

    def _identity_mapping(
        self, mapping_id: str, user_scope_id: str
    ) -> IdentityMapping:
        stored = self.repository.find_object("IdentityMapping", mapping_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced identity mapping does not exist",
                field_errors=[FieldError(
                    path="identity_mapping_ids", code="reference", message="unknown mapping",
                )],
            )
        mapping = IdentityMapping.model_validate_json(stored.payload_bytes)
        if (
            mapping.declared_by_user_scope_id is not None
            and mapping.declared_by_user_scope_id != user_scope_id
        ):
            # Another player's declaration is indistinguishable from a missing
            # one; a null scope is a public (verified) mapping.
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "the referenced identity mapping is not available",
            )
        return mapping

    def _require_game_context(
        self, game_context_id: str, user_scope_id: str
    ) -> None:
        """The context must exist *and* be consumable by this user.

        A context bound to an installation belongs to that installation's
        owner; another user's context is indistinguishable from a missing one.
        A context with no installation is a public-collection context and is
        readable by anyone, exactly as the game catalog is.
        """
        stored = self.repository.find_object("GameContext", game_context_id)
        if stored is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the referenced game context does not exist",
                field_errors=[FieldError(
                    path="game_context_id", code="reference", message="unknown context",
                )],
            )
        installation_id = stored.payload.get("installation_id")
        if installation_id is None:
            return
        with self.repository.unit_of_work() as uow:
            installation = uow.latest_for_series("GameInstallation", installation_id)
        if installation is None:
            raise ContractError(
                ErrorCode.REFERENCE_INVALID,
                "the context names an installation that does not exist",
                field_errors=[FieldError(
                    path="game_context_id", code="reference",
                    message="unknown installation",
                )],
            )
        if installation.payload.get("user_scope_id") != user_scope_id:
            raise ContractError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "the referenced game context is not available",
            )

    def _legacy_run_id(self, frozen: FrozenGroup) -> str | None:
        try:
            data = read_frozen_bytes(frozen, self.files).get("run.json")
            run = json.loads(data.decode("utf-8")) if data is not None else None
        except (ContractError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return run.get("id") if isinstance(run, dict) else None


# --- research format mapping (IMP-T04) ---------------------------------------
#
# The four research documents are *frozen originals*, never product measurements.
# A research format is admitted to the registry only when all three exist:
#   1. an explicit per-field mapping table (``RESEARCH_MAPPING_TABLES``),
#   2. an independent annotation fixture (``ResearchAnnotation``), and
#   3. that annotation's manifest (fixture id + bytes hash) matches the bytes.
# When any of them is missing the document is archived as ``retained_only``:
# the original bytes are kept and no structured record is published.
#
# This repository's two ``*-checks.json`` files state in their own metadata that
# they were reviewed by the same session model (``review_method`` /
# ``interpretation``).  Such a recheck is recorded as ``model_recheck`` and is
# never treated as an independent annotation, so every research format stays
# unregistered by default.

RESEARCH_RESULT_FORMAT_ID = "research-result/v1"
RESEARCH_SOURCE_SECTIONS_FORMAT_ID = "research-source-sections/v1"
RESEARCH_CHECKS_FORMAT_ID = "research-checks/v1"
RESEARCH_ARCHIVE_MAPPING_VERSION = "research-archive/v1"
RESEARCH_RULE_VERSION = "research-import/1"
RESEARCH_IMPORT_ROUTE = "/internal/research-import-publish"

RESEARCH_RECORD_TYPE = "research_observation"


class ResearchFieldMapping(StrictModel):
    """One row of a format's explicit source → product field mapping table."""

    source_path: str
    product_field: str
    value_type: str
    unit: str | None
    location: str


class ResearchAnnotation(StrictModel):
    """The independent annotation gate for one research format.

    ``reviewer_is_session_model`` is not decoration: a same-session model
    recheck is exactly what the checks files already are, and it must never be
    promoted to an independent annotation (08 §4, VAL-V04).
    """

    format_id: str
    source_file: str
    fixture_id: str
    fixture_sha256: Hash
    annotation_method: Literal["independent", "model_recheck"]
    reviewer_source: str
    reviewer_is_session_model: bool
    field_coverage: list[str]

    @field_validator("format_id", "source_file", "fixture_id", "reviewer_source")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("annotation identity fields must not be blank")
        return value


def _fm(
    source_path: str,
    product_field: str,
    value_type: str,
    unit: str | None = None,
) -> ResearchFieldMapping:
    return ResearchFieldMapping(
        source_path=source_path,
        product_field=product_field,
        value_type=value_type,
        unit=unit,
        location="json path in the frozen original",
    )


RESEARCH_MAPPING_TABLES: dict[str, tuple[ResearchFieldMapping, ...]] = {
    RESEARCH_RESULT_FORMAT_ID: (
        _fm("run.date", "run_date", "string"),
        _fm("sources[].id", "source_local_id", "string"),
        _fm("sources[].url", "source_url", "string"),
        _fm("sources[].author", "source_author", "string"),
        _fm("sources[].published_date", "source_published_date", "string"),
        _fm("sources[].retrieved_date", "source_retrieved_date", "string"),
        _fm("sources[].locators.<key>", "source_locator", "string"),
        _fm("recommendation.id", "recommendation_local_id", "string"),
        _fm("recommendation.source_id", "recommendation_source_id", "string"),
        _fm("recommendation.type", "recommendation_type", "string"),
        _fm("recommendation.configuration_kind", "recommendation_configuration_kind", "string"),
        _fm("recommendation.locator_key", "recommendation_locator_key", "string"),
        _fm("recommendation.fixed_settings[].row", "recommendation_fixed_row", "integer"),
        _fm("recommendation.fixed_settings[].setting", "recommendation_fixed_setting", "string"),
        _fm("recommendation.fixed_settings[].value", "recommendation_fixed_value", "string"),
        _fm("recommendation.conditional_policies[].id", "conditional_policy_id", "string"),
        _fm("recommendation.conditional_policies[].policy", "conditional_policy_rule", "string"),
        _fm(
            "recommendation.conditional_policies[].selected_value",
            "conditional_policy_selected_value", "null",
        ),
        _fm("recommendation.missing_fields[]", "recommendation_missing_field", "string"),
        _fm(
            "recommendation.measured_average_fps",
            "recommendation_measured_average_fps", "null", "fps",
        ),
        _fm("ts_environments[].<key>", "ts_environment_field", "any"),
        _fm("wc_hardware.<key>", "wc_hardware_field", "any"),
        _fm("wc_contexts[].<key>", "wc_context_field", "any"),
        _fm("wc_result_environments[].<key>", "wc_result_environment_field", "any"),
        _fm("configurations[].id", "configuration_local_id", "string"),
        _fm("configurations[].kind", "configuration_kind", "string"),
        _fm("configurations[].label", "configuration_label", "string"),
        _fm("configurations[].locator", "configuration_locator", "string"),
        _fm("configurations[].expanded_settings", "configuration_expanded_settings", "null"),
        _fm("measurements[].id", "measurement_local_id", "string"),
        _fm("measurements[].config_id", "measurement_configuration_id", "string"),
        _fm("measurements[].environment_id", "measurement_environment_id", "string"),
        _fm("measurements[].source_row", "measurement_source_row", "string"),
        _fm("measurements[].average_fps", "measurement_average_fps", "number", "fps"),
        _fm("measurements[].one_percent_low", "measurement_one_percent_low", "number", "fps"),
        _fm(
            "measurements[].point_one_percent_low",
            "measurement_point_one_percent_low", "number", "fps",
        ),
        _fm("measurement_semantics.origin", "measurement_origin", "string"),
        _fm(
            "measurement_semantics.independently_reproduced",
            "measurement_independently_reproduced", "boolean",
        ),
        _fm("measurement_semantics.raw_logs_obtained", "measurement_raw_logs_obtained", "boolean"),
        _fm("measurement_semantics.low_metric_calculation_definition",
            "measurement_low_metric_definition", "null"),
        _fm("comparisons[].id", "comparison_local_id", "string"),
        _fm("comparisons[].before", "comparison_before", "string"),
        _fm("comparisons[].after", "comparison_after", "string"),
        _fm(
            "comparisons[].source_reported_improvement_percent.<metric>",
            "comparison_reported_improvement", "number", "percent",
        ),
        _fm("comparisons[].single_setting_causal_claim", "comparison_single_setting_causal", "boolean"),
        _fm("exclusions.<key>", "exclusion_scope", "string"),
    ),
    RESEARCH_SOURCE_SECTIONS_FORMAT_ID: (
        _fm("sources[].id", "source_local_id", "string"),
        _fm("sources[].url", "source_url", "string"),
        _fm("sources[].author", "source_author", "string"),
        _fm("sources[].published_date", "source_published_date", "string"),
        _fm("sources[].updated_date", "source_updated_date", "string"),
        _fm("sources[].retrieved_date", "source_retrieved_date", "string"),
        _fm("sources[].locators.<key>", "source_locator", "string"),
        _fm("dot.configuration_id", "dot_configuration_id", "string"),
        _fm("dot.kind", "dot_kind", "string"),
        _fm("dot.source_id", "dot_source_id", "string"),
        _fm("dot.graphics_locator", "dot_graphics_locator", "string"),
        _fm("dot.graphics_settings[].setting", "dot_graphics_setting", "string"),
        _fm("dot.graphics_settings[].value", "dot_graphics_value", "string"),
        _fm("dot.video_selected_fixed[].setting", "dot_video_setting", "string"),
        _fm("dot.video_selected_fixed[].value", "dot_video_value", "string"),
        _fm("dot.video_nonfixed[].setting", "dot_nonfixed_setting", "string"),
        _fm("dot.video_nonfixed[].type", "dot_nonfixed_type", "string"),
        _fm("dot.video_nonfixed[].policy", "dot_nonfixed_policy", "string"),
        _fm(
            "dot.video_nonfixed[].author_observed_resolution",
            "dot_observed_resolution", "string",
        ),
        _fm("dot.video_nonfixed[].universal_fixed_value", "dot_universal_fixed_value", "null"),
        _fm("dot.video_nonfixed[].reported_state", "hdr_reported_state", "string"),
        _fm("dot.video_nonfixed[].recommended_switch", "hdr_recommended_switch", "null"),
        _fm("dot.coverage.<key>", "dot_coverage", "any"),
        _fm("dot.environment.<key>", "dot_environment_field", "any"),
        _fm("dot.observation.source_id", "observation_source_id", "string"),
        _fm("dot.observation.locator_key", "observation_locator_key", "string"),
        _fm("dot.observation.configuration_id", "observation_configuration_id", "string"),
        _fm("dot.observation.binding", "observation_binding", "string"),
        _fm("dot.observation.type", "observation_type", "string"),
        _fm("dot.observation.value_min", "fps_lower", "number", "fps"),
        _fm("dot.observation.value_max", "fps_upper", "number", "fps"),
        _fm("dot.observation.unit", "fps_unit", "string"),
        _fm("dot.observation.scene", "observation_scene", "string"),
        _fm("dot.observation.aggregation_method", "fps_aggregation_method", "null"),
        _fm("dot.observation.average_fps", "fps_average", "null", "fps"),
        _fm("dot.observation.one_percent_low", "fps_one_percent_low", "null", "fps"),
        _fm("dot.observation.duration_seconds", "fps_duration_seconds", "null", "second"),
        _fm(
            "dot.observation.independently_reproduced",
            "observation_independently_reproduced", "boolean",
        ),
        _fm("gg.source_id", "gg_source_id", "string"),
        _fm("gg.configuration_id", "gg_configuration_id", "string"),
        _fm("gg.configuration_kind", "gg_configuration_kind", "string"),
        _fm("gg.locator_key", "gg_locator_key", "string"),
        _fm("gg.columns[].id", "gg_column_id", "string"),
        _fm("gg.columns[].examples[]", "gg_column_example", "string"),
        _fm(
            "gg.columns[].examples_are_actual_benchmark_hardware",
            "gg_column_examples_are_benchmark_hardware", "boolean",
        ),
        _fm("gg.matrix_rows[].setting", "gg_matrix_setting", "string"),
        _fm("gg.matrix_rows[].values[]", "gg_matrix_value", "string"),
        _fm("gg.conditional_cells[].selected_value", "gg_conditional_selected_value", "null"),
        _fm("gg.coverage.<key>", "gg_coverage", "any"),
        _fm("gg.benchmark_context.<key>", "gg_benchmark_context", "any"),
        _fm("gg.fps_claims[].mode", "gg_fps_claim_mode", "string"),
        _fm("gg.fps_claims[].reported_fps", "gg_reported_fps", "number", "fps"),
        _fm("gg.fps_claims[].metric_type", "gg_fps_metric_type", "string"),
        _fm("gg.fps_claims[].average_fps", "gg_fps_claim_average", "null", "fps"),
        _fm("gg.fps_claims_binding", "gg_fps_claims_binding", "string"),
    ),
    # The checks documents are historical evidence only.  Every field is
    # deliberately unmapped: copying a check's ``match: true`` into a product
    # check result would create a new knowledge fact from a past self-review.
    RESEARCH_CHECKS_FORMAT_ID: (),
}

RESEARCH_MAPPING_NOTES: dict[str, str] = {
    RESEARCH_RESULT_FORMAT_ID: (
        "author claims and source measurements are research observations;"
        " they are never PerformanceSummary values or recommendation actions"
    ),
    RESEARCH_SOURCE_SECTIONS_FORMAT_ID: (
        "the dot and gg sections are separate namespaces; a local id is only"
        " comparable inside its own section"
    ),
    RESEARCH_CHECKS_FORMAT_ID: (
        "historical check evidence only: the frozen original is registered,"
        " no check result and no knowledge record is created or rewritten"
    ),
}


def _mapping_fields(format_id: str) -> set[str]:
    return {row.product_field for row in RESEARCH_MAPPING_TABLES.get(format_id, ())}


def research_annotation_problems(annotation: ResearchAnnotation) -> list[str]:
    """Why this annotation does not admit a mapper (empty means it does)."""

    problems: list[str] = []
    if annotation.annotation_method != "independent":
        problems.append(
            "annotation_method is model_recheck; a same-model recheck is not an"
            " independent annotation"
        )
    if annotation.reviewer_is_session_model:
        problems.append(
            "the reviewer is this session's model; that is a consistency check,"
            " not independent annotation"
        )
    table = RESEARCH_MAPPING_TABLES.get(annotation.format_id)
    if table is None:
        problems.append(f"no field mapping table exists for {annotation.format_id!r}")
    else:
        missing = sorted(_mapping_fields(annotation.format_id) - set(annotation.field_coverage))
        if missing:
            problems.append(
                "the annotation does not cover mapped field(s): " + ", ".join(missing)
            )
    return problems


def _detect_research_format(data: bytes) -> tuple[str | None, str]:
    """Recognise a research document from top-level structure, never its name."""

    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, f"the document is not UTF-8 JSON ({type(error).__name__})"
    if not isinstance(payload, dict):
        return None, "a research document is a JSON object"
    if "review_method" in payload and "counts" in payload:
        return None, (
            "a review summary with no verified field mapping; it is kept as a"
            " retained original rather than guessed at"
        )
    if all(key in payload for key in (
        "record_version", "run", "sources", "recommendation", "measurements",
    )):
        return RESEARCH_RESULT_FORMAT_ID, ""
    if (
        isinstance(payload.get("run"), dict)
        and isinstance(payload.get("sources"), list)
        and ("dot" in payload or "gg" in payload)
    ):
        return RESEARCH_SOURCE_SECTIONS_FORMAT_ID, ""
    if "extraction_sha256" in payload and any(
        key in payload for key in ("metric_checks", "row_checks", "policy_checks", "scope_checks")
    ):
        return RESEARCH_CHECKS_FORMAT_ID, ""
    return None, "the top-level structure matches no registered research format"


def _annotated_research_probe(
    annotation: ResearchAnnotation,
) -> Callable[[Mapping[str, bytes]], ProbeResult]:
    def probe(files: Mapping[str, bytes]) -> ProbeResult:
        data = files.get(annotation.source_file)
        if data is None:
            return ProbeResult(
                False, [f"{annotation.source_file} is not part of this group"], []
            )
        if hashlib.sha256(data).hexdigest() != annotation.fixture_sha256:
            return ProbeResult(
                False,
                ["the file bytes do not match the annotated fixture; the mapper"
                 " only runs over the annotated fixture"],
                [],
            )
        detected, reason = _detect_research_format(data)
        if detected != annotation.format_id:
            return ProbeResult(False, [reason or "another research format was detected"], [])
        return ProbeResult(True, [], [])

    return probe


def research_format_specs(
    annotations: Sequence[ResearchAnnotation],
) -> list[FormatSpec]:
    """Only fully annotated formats become probes; anything less stays out."""

    specs: list[FormatSpec] = []
    registered: set[str] = set()
    for annotation in annotations:
        if annotation.format_id in registered:
            continue
        if research_annotation_problems(annotation):
            continue
        registered.add(annotation.format_id)
        specs.append(FormatSpec(
            format_id=annotation.format_id,
            required_files=(annotation.source_file,),
            optional_files=(),
            check=_annotated_research_probe(annotation),
        ))
    return specs


def research_format_registry(
    annotations: Sequence[ResearchAnnotation] = (),
) -> FormatRegistry:
    """The collector format plus only the research formats that passed the gate."""

    registry = default_format_registry()
    for spec in research_format_specs(annotations):
        registry.register(spec)
    return registry


@dataclass(frozen=True)
class ResearchFrozenDocument:
    relative_name: str
    file_id: str
    manifest: FileManifest
    sha256: str


@dataclass(frozen=True)
class ResearchMapping:
    format_id: str
    sources: list[Source]
    source_revisions: list[SourceRevision]
    records: list[ImportedRecord]
    diagnostic_record_ids: list[str]
    limitations: list[str]


@dataclass(frozen=True)
class ResearchImportResult:
    snapshot_id: str
    code: str
    format_id: str | None
    annotation_status: str
    accepted_record_ids: list[str]
    diagnostic_record_ids: list[str]
    limitations: list[str]
    refs: list[ObjectRef]


def _research_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _observation(
    product_field: str,
    value: Any,
    *,
    file_name: str,
    path: str,
    block: str,
    unit: str | None = None,
    boundary: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """One explicitly located field.  A null value stays a stated unknown."""

    observation: dict[str, Any] = {
        "product_field": product_field,
        "value": value,
        "value_type": _research_type_name(value),
        "unit": unit,
        "boundary_semantics": boundary,
        "source_location": {"file": file_name, "path": path, "block": block},
    }
    if note is not None:
        observation["note"] = note
    return observation


def _research_timestamp(value: Any) -> str | None:
    """A date-only research value is anchored at midnight UTC, not given an hour."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return f"{text}T00:00:00+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return text


def _research_sources(
    payload: Mapping[str, Any],
    *,
    snapshot_id: str,
    relative_name: str,
    clock: Callable[[], str],
) -> tuple[list[Source], list[SourceRevision], dict[str, str], list[str]]:
    sources: list[Source] = []
    revisions: list[SourceRevision] = []
    revision_by_source: dict[str, str] = {}
    limitations: list[str] = []
    rows = payload.get("sources")
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    fallback_date = run.get("date") if isinstance(run, dict) else None
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            limitations.append(f"sources[{index}] is not an object and is not mapped")
            continue
        source_id = row.get("id")
        url = row.get("url")
        if not _is_text(source_id) or not _is_text(url):
            limitations.append(
                f"sources[{index}] has no id/url; it is not mapped into a Source"
            )
            continue
        if source_id in revision_by_source:
            limitations.append(
                f"source {source_id!r} is repeated; each occurrence is not merged"
            )
            continue
        retrieved_at = _research_timestamp(row.get("retrieved_date") or fallback_date)
        if retrieved_at is None:
            limitations.append(
                f"source {source_id!r} states no retrieval timestamp; its revision"
                " carries no document and its retrieval time is not invented"
            )
            retrieved_at = "1970-01-01T00:00:00+00:00"
        source = Source(
            id=_import_id(snapshot_id, "research_source", source_id),
            schema_version="product-contract/0.4",
            created_at=clock(),
            created_by_task_id=None,
            original_url=url,
            canonical_url=canonical_source_url(url),
            identity_checks=[_check(
                "source.identity",
                CheckStatus.not_run,
                "the research extraction names an author and URL; neither proves"
                " an independent test",
            )],
        )
        revision = SourceRevision(
            id=_import_id(snapshot_id, "research_source_revision", source_id),
            schema_version="product-contract/0.4",
            created_at=clock(),
            created_by_task_id=None,
            source_id=source.id,
            retrieved_at=retrieved_at,
            upstream_revision_id=None,
            content_hash=None,
            document_file_id=None,
            capture_method="research_extraction_manifest",
            capture_scope=source_id,
            metadata=SourceMetadata(
                author=row.get("author"),
                published_date=row.get("published_date"),
                updated_date=row.get("updated_date"),
                test_date=None,
                original_source_url=url,
            ),
            truncated=False,
            checks=[_check(
                "source.document",
                CheckStatus.not_run,
                "the research extraction states no fetched document hash; only"
                " this manifest metadata is preserved",
            )],
        )
        sources.append(source)
        revisions.append(revision)
        revision_by_source[source_id] = revision.id
    return sources, revisions, revision_by_source, limitations


def _file_revision(
    *,
    snapshot_id: str,
    relative_name: str,
    clock: Callable[[], str],
) -> tuple[Source, SourceRevision]:
    """A revision anchored on the frozen document itself, not on a web source."""

    url = f"urn:research-file:{relative_name}"
    source = Source(
        id=_import_id(snapshot_id, "research_file_source", relative_name),
        schema_version="product-contract/0.4",
        created_at=clock(),
        created_by_task_id=None,
        original_url=url,
        canonical_url=url,
        identity_checks=[_check(
            "source.identity",
            CheckStatus.not_run,
            "this is the frozen research document, not a separately captured web source",
        )],
    )
    revision = SourceRevision(
        id=_import_id(snapshot_id, "research_file_revision", relative_name),
        schema_version="product-contract/0.4",
        created_at=clock(),
        created_by_task_id=None,
        source_id=source.id,
        retrieved_at="1970-01-01T00:00:00+00:00",
        upstream_revision_id=None,
        content_hash=None,
        document_file_id=None,
        capture_method="research_extraction_manifest",
        capture_scope=relative_name,
        metadata=SourceMetadata(
            author=None,
            published_date=None,
            updated_date=None,
            test_date=None,
            original_source_url=None,
        ),
        truncated=False,
        checks=[_check(
            "source.document",
            CheckStatus.not_run,
            "the frozen document is the only locator; no retrieval time is stated"
            " and the epoch value is a placeholder, not a capture time",
        )],
    )
    return source, revision


class _ResearchBuilder:
    """Build located observations for one research document."""

    def __init__(
        self,
        *,
        format_id: str,
        section: str | None,
        relative_name: str,
        snapshot_id: str,
        clock: Callable[[], str],
        revision_by_source: Mapping[str, str],
    ) -> None:
        self.format_id = format_id
        self.section = section
        self.relative_name = relative_name
        self.snapshot_id = snapshot_id
        self.clock = clock
        self.revision_by_source = revision_by_source
        self.records: list[ImportedRecord] = []
        self.diagnostic: list[str] = []
        self.limitations: list[str] = []

    @property
    def scope(self) -> str:
        return self.format_id if self.section is None else f"{self.format_id}:{self.section}"

    def record(
        self,
        *,
        local_id: str,
        record_kind: str,
        source_id: str | None,
        fields: list[dict[str, Any]],
        unknowns: Sequence[str] = (),
        limitations: Sequence[str] = (),
        envelope: Any = None,
        quarantined: bool = False,
        reference_problems: Sequence[str] = (),
        extra_checks: Sequence[CheckResult] = (),
    ) -> ImportedRecord:
        prefix = "" if self.section is None else f"{self.section}:"
        local_record_id = f"{prefix}{record_kind}:{local_id}"
        record_id = _import_id(self.snapshot_id, "research_record", self.scope, local_record_id)
        revision_id = self.revision_by_source.get(source_id or "")
        reasons: list[str] = []
        if revision_id is None:
            # The stated source is not in the document.  Anchor the envelope on
            # the frozen file and isolate the record instead of borrowing a
            # differently named source.
            revision_id = self.revision_by_source[""]
            reasons.append(
                f"the stated source {source_id!r} is not declared in this document"
            )
        reasons.extend(reference_problems)
        checks = [
            _check(
                "research.mapping",
                CheckStatus.pass_,
                f"mapped by the explicit {self.format_id} table; every field"
                " carries a source location",
            ),
            _check(
                "research.location",
                CheckStatus.pass_ if all(
                    field.get("source_location") for field in fields
                ) else CheckStatus.fail,
                f"{len(fields)} field(s) with an explicit source location",
            ),
            _check(
                "research.origin",
                CheckStatus.not_run,
                "research observations are never the player's measurement, a"
                " recommendation action or an applied configuration",
            ),
            _check(
                "research.reference",
                CheckStatus.fail if reasons else CheckStatus.pass_,
                "; ".join(reasons) if reasons else "the stated source is declared in this document",
            ),
            *extra_checks,
        ]
        if unknowns:
            checks.append(_check(
                "research.unknown",
                CheckStatus.unknown,
                "preserved unknown(s): " + ", ".join(unknowns),
            ))
        if quarantined or reasons:
            checks.append(_check(
                "research.usable",
                CheckStatus.fail,
                "the observation is isolated; independent observations are kept",
            ))
        payload = {
            "record_kind": record_kind,
            "format_id": self.format_id,
            "section": self.section,
            "source_id": source_id,
            "fields": fields,
            "unknowns": list(unknowns),
            "limitations": list(limitations),
        }
        record = ImportedRecord(
            id=record_id,
            schema_version="product-contract/0.4",
            created_at=self.clock(),
            created_by_task_id=None,
            record_snapshot_id=record_id,
            import_snapshot_id=self.snapshot_id,
            capture_scope_id=self.scope,
            local_record_id=local_record_id,
            legacy_record_id=local_record_id,
            source_revision_id=revision_id,
            record_type=RESEARCH_RECORD_TYPE,
            payload=payload,
            payload_hash=hashlib.sha256(canonical_json(payload)).hexdigest(),
            upstream_status=(
                LegacyRecordState.quarantined
                if quarantined or reasons
                else LegacyRecordState.accepted
            ),
            upstream_envelope=(
                envelope if isinstance(envelope, dict) else {"value": envelope}
            ),
            checks=checks,
            extension=_research_extension(fields),
        )
        self.records.append(record)
        if quarantined or reasons:
            self.diagnostic.append(record.id)
        return record


def _research_extension(fields: Sequence[Mapping[str, Any]]) -> RecordExtension:
    boundary: BoundarySemantics | None = None
    if any(field.get("boundary_semantics") == "unknown" for field in fields):
        boundary = BoundarySemantics(
            lower_inclusive=None,
            upper_inclusive=None,
            evidence_refs=[],
            status=CheckStatus.unknown,
        )
    extra = []
    if boundary is not None:
        extra.append(_check(
            "research.boundary_semantics",
            CheckStatus.unknown,
            "the source states a range without inclusive flags; strictness stays"
            " unknown and no average is computed",
        ))
    return RecordExtension(
        boundary_semantics=boundary,
        setting_identity_ref=None,
        checks=extra,
    )


def _map_research_result(
    payload: Mapping[str, Any],
    *,
    snapshot_id: str,
    relative_name: str,
    clock: Callable[[], str],
) -> ResearchMapping:
    sources, revisions, revision_by_source, limitations = _research_sources(
        payload, snapshot_id=snapshot_id, relative_name=relative_name, clock=clock,
    )
    revisions_by_source = dict(revision_by_source)
    fallback_source, fallback_revision = _file_revision(
        snapshot_id=snapshot_id, relative_name=relative_name, clock=clock,
    )
    revisions_by_source[""] = fallback_revision.id
    builder = _ResearchBuilder(
        format_id=RESEARCH_RESULT_FORMAT_ID,
        section=None,
        relative_name=relative_name,
        snapshot_id=snapshot_id,
        clock=clock,
        revision_by_source=revisions_by_source,
    )
    builder.limitations.extend(limitations)

    recommendation = payload.get("recommendation")
    if isinstance(recommendation, dict):
        fields: list[dict[str, Any]] = []
        for name, path in (
            ("recommendation_local_id", "recommendation.id"),
            ("recommendation_source_id", "recommendation.source_id"),
            ("recommendation_type", "recommendation.type"),
            ("recommendation_configuration_kind", "recommendation.configuration_kind"),
            ("recommendation_locator_key", "recommendation.locator_key"),
            ("recommendation_measured_average_fps", "recommendation.measured_average_fps"),
        ):
            key = path.rsplit(".", 1)[1]
            fields.append(_observation(
                name, recommendation.get(key), file_name=relative_name,
                path=path, block="recommendation",
                unit="fps" if name.endswith("average_fps") else None,
            ))
        for index, item in enumerate(recommendation.get("fixed_settings") or []):
            if not isinstance(item, dict):
                continue
            block = f"recommendation.fixed_settings[{index}]"
            fields.append(_observation(
                "recommendation_fixed_row", item.get("row"), file_name=relative_name,
                path=f"{block}.row", block=block,
            ))
            fields.append(_observation(
                "recommendation_fixed_setting", item.get("setting"),
                file_name=relative_name, path=f"{block}.setting", block=block,
            ))
            fields.append(_observation(
                "recommendation_fixed_value", item.get("value"), file_name=relative_name,
                path=f"{block}.value", block=block,
            ))
        for index, item in enumerate(recommendation.get("conditional_policies") or []):
            if not isinstance(item, dict):
                continue
            block = f"recommendation.conditional_policies[{index}]"
            for name, key in (
                ("conditional_policy_id", "id"),
                ("conditional_policy_rule", "policy"),
                ("conditional_policy_selected_value", "selected_value"),
            ):
                fields.append(_observation(
                    name, item.get(key), file_name=relative_name,
                    path=f"{block}.{key}", block=block,
                ))
        for index, item in enumerate(recommendation.get("missing_fields") or []):
            fields.append(_observation(
                "recommendation_missing_field", item, file_name=relative_name,
                path=f"recommendation.missing_fields[{index}]",
                block="recommendation.missing_fields",
            ))
        builder.record(
            local_id=_is_text(recommendation.get("id")) and recommendation["id"] or "recommendation",
            record_kind="author_recommendation_observation",
            source_id=recommendation.get("source_id"),
            fields=fields,
            unknowns=["conditional_policies[].selected_value"],
            limitations=[
                "an author template is an observation; it is not a recommendation"
                " action and its unselected branches stay unselected",
            ],
            envelope=recommendation,
        )

    for group_name, field_name, block_name in (
        ("ts_environments", "ts_environment_field", "ts_environments"),
        ("wc_contexts", "wc_context_field", "wc_contexts"),
        ("wc_result_environments", "wc_result_environment_field", "wc_result_environments"),
    ):
        for index, item in enumerate(payload.get(group_name) or []):
            if not isinstance(item, dict):
                continue
            local_id = item.get("id") or f"{block_name}.{index}"
            fields = [
                _observation(
                    field_name, value, file_name=relative_name,
                    path=f"{block_name}[{index}].{key}", block=f"{block_name}[{index}]",
                )
                for key, value in item.items()
            ]
            builder.record(
                local_id=local_id,
                record_kind="reported_environment",
                source_id=item.get("source_id"),
                fields=fields,
                unknowns=[key for key, value in item.items() if value is None],
                envelope=item,
            )

    hardware = payload.get("wc_hardware")
    if isinstance(hardware, dict) and hardware:
        builder.record(
            local_id=hardware.get("id") or "wc_hardware",
            record_kind="reported_hardware",
            source_id=hardware.get("source_id"),
            fields=[
                _observation(
                    "wc_hardware_field", value, file_name=relative_name,
                    path=f"wc_hardware.{key}", block="wc_hardware",
                    note=f"source key {key}",
                )
                for key, value in hardware.items()
            ],
            unknowns=[key for key, value in hardware.items() if value is None],
            envelope=hardware,
        )

    configuration_ids = {
        item.get("id") for item in payload.get("configurations") or []
        if isinstance(item, dict) and _is_text(item.get("id"))
    }
    for index, item in enumerate(payload.get("configurations") or []):
        if not isinstance(item, dict):
            continue
        local_id = item.get("id") or f"configuration.{index}"
        builder.record(
            local_id=local_id,
            record_kind="reported_configuration",
            source_id=item.get("source_id"),
            fields=[
                _observation(
                    name, item.get(key), file_name=relative_name,
                    path=f"configurations[{index}].{key}", block=f"configurations[{index}]",
                )
                for name, key in (
                    ("configuration_local_id", "id"),
                    ("configuration_kind", "kind"),
                    ("configuration_label", "label"),
                    ("configuration_locator", "locator"),
                    ("configuration_expanded_settings", "expanded_settings"),
                )
            ],
            unknowns=[key for key in ("expanded_settings",) if item.get(key) is None],
            envelope=item,
        )

    environment_ids = {
        item.get("id")
        for group_name in ("ts_environments", "wc_contexts", "wc_result_environments")
        for item in payload.get(group_name) or []
        if isinstance(item, dict) and _is_text(item.get("id"))
    }
    measurement_ids = {
        item.get("id") for item in payload.get("measurements") or []
        if isinstance(item, dict) and _is_text(item.get("id"))
    }
    for index, item in enumerate(payload.get("measurements") or []):
        if not isinstance(item, dict):
            continue
        local_id = item.get("id") or f"measurement.{index}"
        fields = [
            _observation(
                "measurement_local_id", item.get("id"), file_name=relative_name,
                path=f"measurements[{index}].id", block=f"measurements[{index}]",
            ),
            _observation(
                "measurement_configuration_id", item.get("config_id"),
                file_name=relative_name, path=f"measurements[{index}].config_id",
                block=f"measurements[{index}]",
            ),
            _observation(
                "measurement_environment_id", item.get("environment_id"),
                file_name=relative_name, path=f"measurements[{index}].environment_id",
                block=f"measurements[{index}]",
            ),
            _observation(
                "measurement_source_row", item.get("source_row"),
                file_name=relative_name, path=f"measurements[{index}].source_row",
                block=f"measurements[{index}]",
            ),
        ]
        for name, key, unit in (
            ("measurement_average_fps", "average_fps", "fps"),
            ("measurement_one_percent_low", "one_percent_low", "fps"),
            ("measurement_point_one_percent_low", "point_one_percent_low", "fps"),
        ):
            fields.append(_observation(
                name, item.get(key), file_name=relative_name,
                path=f"measurements[{index}].{key}", block=f"measurements[{index}]",
                unit=unit,
            ))
        semantics = payload.get("measurement_semantics")
        if isinstance(semantics, dict):
            fields.extend([
                _observation(
                    "measurement_origin", semantics.get("origin"),
                    file_name=relative_name, path="measurement_semantics.origin",
                    block="measurement_semantics",
                ),
                _observation(
                    "measurement_independently_reproduced",
                    semantics.get("independently_reproduced"),
                    file_name=relative_name,
                    path="measurement_semantics.independently_reproduced",
                    block="measurement_semantics",
                ),
                _observation(
                    "measurement_raw_logs_obtained", semantics.get("raw_logs_obtained"),
                    file_name=relative_name, path="measurement_semantics.raw_logs_obtained",
                    block="measurement_semantics",
                ),
                _observation(
                    "measurement_low_metric_definition",
                    semantics.get("low_metric_calculation_definition"),
                    file_name=relative_name,
                    path="measurement_semantics.low_metric_calculation_definition",
                    block="measurement_semantics",
                ),
            ])
        reasons = []
        if item.get("config_id") not in configuration_ids:
            reasons.append(
                f"measurements[{index}].config_id {item.get('config_id')!r} is not"
                " a configuration in this document"
            )
        if item.get("environment_id") not in environment_ids:
            reasons.append(
                f"measurements[{index}].environment_id {item.get('environment_id')!r}"
                " is not an environment in this document"
            )
        record = builder.record(
            local_id=local_id,
            record_kind="source_measurement",
            source_id=item.get("source_id"),
            fields=fields,
            unknowns=[key for key, value in item.items() if value is None],
            limitations=[
                "the value is the source author's measurement, not the player's;"
                " it is not a PerformanceSummary",
            ],
            envelope=item,
            quarantined=bool(reasons),
            reference_problems=reasons,
        )
        if reasons:
            builder.limitations.extend(reasons)

    for index, item in enumerate(payload.get("comparisons") or []):
        if not isinstance(item, dict):
            continue
        local_id = item.get("id") or f"comparison.{index}"
        fields = [
            _observation(
                "comparison_local_id", item.get("id"), file_name=relative_name,
                path=f"comparisons[{index}].id", block=f"comparisons[{index}]",
            ),
            _observation(
                "comparison_before", item.get("before"), file_name=relative_name,
                path=f"comparisons[{index}].before", block=f"comparisons[{index}]",
            ),
            _observation(
                "comparison_after", item.get("after"), file_name=relative_name,
                path=f"comparisons[{index}].after", block=f"comparisons[{index}]",
            ),
            _observation(
                "comparison_single_setting_causal", item.get("single_setting_causal_claim"),
                file_name=relative_name,
                path=f"comparisons[{index}].single_setting_causal_claim",
                block=f"comparisons[{index}]",
            ),
        ]
        improvements = item.get("source_reported_improvement_percent")
        if isinstance(improvements, dict):
            for metric, value in improvements.items():
                fields.append(_observation(
                    "comparison_reported_improvement", value,
                    file_name=relative_name,
                    path=f"comparisons[{index}].source_reported_improvement_percent.{metric}",
                    block=f"comparisons[{index}].source_reported_improvement_percent",
                    unit="percent", note=f"metric {metric}; a source-reported claim",
                ))
        reasons = [
            f"comparisons[{index}].{key} {item.get(key)!r} is not a measurement in"
            " this document"
            for key in ("before", "after")
            if item.get(key) not in measurement_ids
        ]
        builder.record(
            local_id=local_id,
            record_kind="reported_comparison",
            source_id=None,
            fields=fields,
            limitations=["a source-reported comparison is not a local experiment"],
            envelope=item,
            quarantined=bool(reasons),
            reference_problems=reasons,
        )
        builder.limitations.extend(reasons)

    exclusions = payload.get("exclusions")
    if isinstance(exclusions, dict) and exclusions:
        builder.record(
            local_id="exclusions",
            record_kind="exclusion_scope",
            source_id=None,
            fields=[
                _observation(
                    "exclusion_scope", value, file_name=relative_name,
                    path=f"exclusions.{key}", block="exclusions", note=f"source key {key}",
                )
                for key, value in exclusions.items()
            ],
            envelope=exclusions,
        )

    builder.limitations.append(RESEARCH_MAPPING_NOTES[RESEARCH_RESULT_FORMAT_ID])
    return ResearchMapping(
        format_id=RESEARCH_RESULT_FORMAT_ID,
        sources=[*sources, fallback_source],
        source_revisions=[*revisions, fallback_revision],
        records=builder.records,
        diagnostic_record_ids=builder.diagnostic,
        limitations=builder.limitations,
    )


def _map_research_source_sections(
    payload: Mapping[str, Any],
    *,
    snapshot_id: str,
    relative_name: str,
    clock: Callable[[], str],
) -> ResearchMapping:
    sources, revisions, revision_by_source, limitations = _research_sources(
        payload, snapshot_id=snapshot_id, relative_name=relative_name, clock=clock,
    )
    revisions_by_source = dict(revision_by_source)
    fallback_source, fallback_revision = _file_revision(
        snapshot_id=snapshot_id, relative_name=relative_name, clock=clock,
    )
    revisions_by_source[""] = fallback_revision.id
    all_sources = [*sources, fallback_source]
    all_revisions = [*revisions, fallback_revision]
    records: list[ImportedRecord] = []
    diagnostic: list[str] = []
    all_limitations = list(limitations)

    dot = payload.get("dot")
    if isinstance(dot, dict):
        builder = _ResearchBuilder(
            format_id=RESEARCH_SOURCE_SECTIONS_FORMAT_ID,
            section="dot",
            relative_name=relative_name,
            snapshot_id=snapshot_id,
            clock=clock,
            revision_by_source=revisions_by_source,
        )
        fields: list[dict[str, Any]] = [
            _observation(
                "dot_configuration_id", dot.get("configuration_id"),
                file_name=relative_name, path="dot.configuration_id", block="dot",
            ),
            _observation(
                "dot_kind", dot.get("kind"), file_name=relative_name,
                path="dot.kind", block="dot",
            ),
            _observation(
                "dot_source_id", dot.get("source_id"), file_name=relative_name,
                path="dot.source_id", block="dot",
            ),
            _observation(
                "dot_graphics_locator", dot.get("graphics_locator"),
                file_name=relative_name, path="dot.graphics_locator", block="dot",
            ),
        ]
        for index, item in enumerate(dot.get("graphics_settings") or []):
            if not isinstance(item, dict):
                continue
            block = f"dot.graphics_settings[{index}]"
            fields.extend([
                _observation(
                    "dot_graphics_setting", item.get("setting"),
                    file_name=relative_name, path=f"{block}.setting", block=block,
                ),
                _observation(
                    "dot_graphics_value", item.get("value"),
                    file_name=relative_name, path=f"{block}.value", block=block,
                ),
            ])
        for index, item in enumerate(dot.get("video_selected_fixed") or []):
            if not isinstance(item, dict):
                continue
            block = f"dot.video_selected_fixed[{index}]"
            fields.extend([
                _observation(
                    "dot_video_setting", item.get("setting"),
                    file_name=relative_name, path=f"{block}.setting", block=block,
                ),
                _observation(
                    "dot_video_value", item.get("value"),
                    file_name=relative_name, path=f"{block}.value", block=block,
                ),
            ])
        coverage = dot.get("coverage")
        if isinstance(coverage, dict):
            fields.extend(
                _observation(
                    "dot_coverage", value, file_name=relative_name,
                    path=f"dot.coverage.{key}", block="dot.coverage",
                    note=f"source key {key}",
                )
                for key, value in coverage.items()
            )
        builder.record(
            local_id=_is_text(dot.get("configuration_id"))
            and dot["configuration_id"] or "dot_configuration",
            record_kind="source_configuration_section",
            source_id=dot.get("source_id"),
            fields=fields,
            unknowns=[
                "dot.video_nonfixed (kept as separate observations)",
            ],
            limitations=[
                "the table is a source recommendation; runtime completeness is"
                " not claimed",
            ],
            envelope={
                key: value for key, value in dot.items()
                if key not in ("video_nonfixed", "environment", "observation")
            },
        )

        for index, item in enumerate(dot.get("video_nonfixed") or []):
            if not isinstance(item, dict):
                continue
            block = f"dot.video_nonfixed[{index}]"
            is_hdr = item.get("type") == "availability_observation"
            if is_hdr:
                hdr_fields = [
                    _observation(
                        "hdr_reported_state", item.get("reported_state"),
                        file_name=relative_name, path=f"{block}.reported_state",
                        block=block,
                    ),
                    _observation(
                        "hdr_recommended_switch", item.get("recommended_switch"),
                        file_name=relative_name, path=f"{block}.recommended_switch",
                        block=block,
                        note="null means the source recommends no switch; it is not"
                             " a brightness or contrast value",
                    ),
                    _observation(
                        "dot_nonfixed_type", item.get("type"),
                        file_name=relative_name, path=f"{block}.type", block=block,
                    ),
                ]
                builder.record(
                    local_id="hdr",
                    record_kind="hdr_availability_observation",
                    source_id=dot.get("source_id"),
                    fields=hdr_fields,
                    unknowns=["hdr_recommended_switch"],
                    limitations=[
                        "HDR is an availability observation and stays outside the"
                        " graphics values; it is not a brightness/contrast reading",
                    ],
                    envelope=item,
                )
            else:
                builder.record(
                    local_id=str(item.get("setting") or f"nonfixed.{index}"),
                    record_kind="display_policy_observation",
                    source_id=dot.get("source_id"),
                    fields=[
                        _observation(
                            "dot_nonfixed_setting", item.get("setting"),
                            file_name=relative_name, path=f"{block}.setting",
                            block=block,
                        ),
                        _observation(
                            "dot_nonfixed_type", item.get("type"),
                            file_name=relative_name, path=f"{block}.type", block=block,
                        ),
                        _observation(
                            "dot_nonfixed_policy", item.get("policy"),
                            file_name=relative_name, path=f"{block}.policy",
                            block=block,
                        ),
                        _observation(
                            "dot_observed_resolution",
                            item.get("author_observed_resolution"),
                            file_name=relative_name,
                            path=f"{block}.author_observed_resolution", block=block,
                        ),
                        _observation(
                            "dot_universal_fixed_value", item.get("universal_fixed_value"),
                            file_name=relative_name,
                            path=f"{block}.universal_fixed_value", block=block,
                        ),
                    ],
                    unknowns=[key for key, value in item.items() if value is None],
                    envelope=item,
                )

        environment = dot.get("environment")
        if isinstance(environment, dict):
            builder.record(
                local_id="dot_environment",
                record_kind="reported_environment",
                source_id=dot.get("source_id"),
                fields=[
                    _observation(
                        "dot_environment_field", value, file_name=relative_name,
                        path=f"dot.environment.{key}", block="dot.environment",
                        note=f"source key {key}",
                    )
                    for key, value in environment.items()
                ],
                unknowns=[key for key, value in environment.items() if value is None],
                envelope=environment,
            )

        observation = dot.get("observation")
        if isinstance(observation, dict):
            fields = [
                _observation(
                    "observation_source_id", observation.get("source_id"),
                    file_name=relative_name, path="dot.observation.source_id",
                    block="dot.observation",
                ),
                _observation(
                    "observation_locator_key", observation.get("locator_key"),
                    file_name=relative_name, path="dot.observation.locator_key",
                    block="dot.observation",
                ),
                _observation(
                    "observation_configuration_id", observation.get("configuration_id"),
                    file_name=relative_name, path="dot.observation.configuration_id",
                    block="dot.observation",
                ),
                _observation(
                    "observation_binding", observation.get("binding"),
                    file_name=relative_name, path="dot.observation.binding",
                    block="dot.observation",
                ),
                _observation(
                    "observation_type", observation.get("type"),
                    file_name=relative_name, path="dot.observation.type",
                    block="dot.observation",
                ),
                _observation(
                    "observation_scene", observation.get("scene"),
                    file_name=relative_name, path="dot.observation.scene",
                    block="dot.observation",
                ),
                _observation(
                    "fps_lower", observation.get("value_min"), file_name=relative_name,
                    path="dot.observation.value_min", block="dot.observation",
                    unit="fps", boundary="unknown",
                    note="the source states a range but not inclusive/exclusive flags",
                ),
                _observation(
                    "fps_upper", observation.get("value_max"), file_name=relative_name,
                    path="dot.observation.value_max", block="dot.observation",
                    unit="fps", boundary="unknown",
                    note="the source states a range but not inclusive/exclusive flags",
                ),
                _observation(
                    "fps_unit", observation.get("unit"), file_name=relative_name,
                    path="dot.observation.unit", block="dot.observation",
                ),
                _observation(
                    "fps_aggregation_method", observation.get("aggregation_method"),
                    file_name=relative_name, path="dot.observation.aggregation_method",
                    block="dot.observation",
                ),
                _observation(
                    "fps_average", observation.get("average_fps"),
                    file_name=relative_name, path="dot.observation.average_fps",
                    block="dot.observation", unit="fps",
                    note="null means the source states no average; 77.5 is not"
                         " computed from the bounds",
                ),
                _observation(
                    "fps_one_percent_low", observation.get("one_percent_low"),
                    file_name=relative_name, path="dot.observation.one_percent_low",
                    block="dot.observation", unit="fps",
                ),
                _observation(
                    "fps_duration_seconds", observation.get("duration_seconds"),
                    file_name=relative_name, path="dot.observation.duration_seconds",
                    block="dot.observation", unit="second",
                ),
                _observation(
                    "observation_independently_reproduced",
                    observation.get("independently_reproduced"),
                    file_name=relative_name,
                    path="dot.observation.independently_reproduced",
                    block="dot.observation",
                ),
            ]
            reasons = []
            if observation.get("configuration_id") != dot.get("configuration_id"):
                reasons.append(
                    "dot.observation.configuration_id does not name"
                    " dot.configuration_id; the two are kept unmerged"
                )
            builder.record(
                local_id="fps_interval",
                record_kind="fps_interval",
                source_id=observation.get("source_id") or dot.get("source_id"),
                fields=fields,
                unknowns=[
                    "fps_aggregation_method",
                    "fps_average",
                    "fps_one_percent_low",
                    "fps_duration_seconds",
                ],
                limitations=[
                    "an interval is not an average: lower/upper are preserved with"
                    " unknown boundary semantics and no midpoint is computed",
                ],
                envelope=observation,
                quarantined=bool(reasons),
                reference_problems=reasons,
            )
            builder.limitations.extend(reasons)

        builder.limitations.append(RESEARCH_MAPPING_NOTES[RESEARCH_SOURCE_SECTIONS_FORMAT_ID])
        records.extend(builder.records)
        diagnostic.extend(builder.diagnostic)
        all_limitations.extend(builder.limitations)

    gg = payload.get("gg")
    if isinstance(gg, dict):
        builder = _ResearchBuilder(
            format_id=RESEARCH_SOURCE_SECTIONS_FORMAT_ID,
            section="gg",
            relative_name=relative_name,
            snapshot_id=snapshot_id,
            clock=clock,
            revision_by_source=revisions_by_source,
        )
        columns = [item for item in gg.get("columns") or [] if isinstance(item, dict)]
        fields = [
            _observation(
                "gg_source_id", gg.get("source_id"), file_name=relative_name,
                path="gg.source_id", block="gg",
            ),
            _observation(
                "gg_configuration_kind", gg.get("configuration_kind"),
                file_name=relative_name, path="gg.configuration_kind", block="gg",
            ),
            _observation(
                "gg_locator_key", gg.get("locator_key"), file_name=relative_name,
                path="gg.locator_key", block="gg",
            ),
        ]
        if "configuration_id" in gg:
            fields.append(_observation(
                "gg_configuration_id", gg.get("configuration_id"),
                file_name=relative_name, path="gg.configuration_id", block="gg",
                note="only present when the source states it",
            ))
        for index, item in enumerate(columns):
            block = f"gg.columns[{index}]"
            fields.append(_observation(
                "gg_column_id", item.get("id"), file_name=relative_name,
                path=f"{block}.id", block=block,
            ))
            for inner, example in enumerate(item.get("examples") or []):
                fields.append(_observation(
                    "gg_column_example", example, file_name=relative_name,
                    path=f"{block}.examples[{inner}]", block=block,
                ))
            fields.append(_observation(
                "gg_column_examples_are_benchmark_hardware",
                item.get("examples_are_actual_benchmark_hardware"),
                file_name=relative_name,
                path=f"{block}.examples_are_actual_benchmark_hardware",
                block=block,
            ))
        for index, item in enumerate(gg.get("matrix_rows") or []):
            if not isinstance(item, dict):
                continue
            block = f"gg.matrix_rows[{index}]"
            fields.append(_observation(
                "gg_matrix_setting", item.get("setting"), file_name=relative_name,
                path=f"{block}.setting", block=block,
            ))
            for inner, value in enumerate(item.get("values") or []):
                column = columns[inner].get("id") if inner < len(columns) else f"column.{inner}"
                fields.append(_observation(
                    "gg_matrix_value", value, file_name=relative_name,
                    path=f"{block}.values[{inner}]", block=block,
                    note=f"column {column!r}",
                ))
        for index, item in enumerate(gg.get("conditional_cells") or []):
            if not isinstance(item, dict):
                continue
            block = f"gg.conditional_cells[{index}]"
            fields.append(_observation(
                "gg_conditional_selected_value", item.get("selected_value"),
                file_name=relative_name, path=f"{block}.selected_value", block=block,
                note=f"setting {item.get('setting')!r}, column {item.get('column')!r}",
            ))
        for key, value in (gg.get("coverage") or {}).items():
            fields.append(_observation(
                "gg_coverage", value, file_name=relative_name,
                path=f"gg.coverage.{key}", block="gg.coverage", note=f"source key {key}",
            ))
        for key, value in (gg.get("benchmark_context") or {}).items():
            fields.append(_observation(
                "gg_benchmark_context", value, file_name=relative_name,
                path=f"gg.benchmark_context.{key}", block="gg.benchmark_context",
                note=f"source key {key}",
            ))
        builder.record(
            local_id=_is_text(gg.get("configuration_id"))
            and gg["configuration_id"] or "tier_matrix",
            record_kind="tier_recommendation_matrix",
            source_id=gg.get("source_id"),
            fields=fields,
            unknowns=[
                "gg.conditional_cells[].selected_value",
                "gg.columns[].examples_are_actual_benchmark_hardware=false",
            ],
            limitations=[
                "tier templates are not measured configurations; examples are not"
                " benchmark hardware unless the source says so",
            ],
            envelope={
                key: value for key, value in gg.items()
                if key not in ("fps_claims", "benchmark_context")
            },
        )

        for index, item in enumerate(gg.get("fps_claims") or []):
            if not isinstance(item, dict):
                continue
            block = f"gg.fps_claims[{index}]"
            builder.record(
                local_id=str(item.get("mode") or f"claim.{index}"),
                record_kind="reported_fps_claim",
                source_id=gg.get("source_id"),
                fields=[
                    _observation(
                        "gg_fps_claim_mode", item.get("mode"),
                        file_name=relative_name, path=f"{block}.mode", block=block,
                    ),
                    _observation(
                        "gg_reported_fps", item.get("reported_fps"),
                        file_name=relative_name, path=f"{block}.reported_fps",
                        block=block, unit="fps",
                        note="the source calls this an unspecified FPS, not an average",
                    ),
                    _observation(
                        "gg_fps_metric_type", item.get("metric_type"),
                        file_name=relative_name, path=f"{block}.metric_type", block=block,
                    ),
                    _observation(
                        "gg_fps_claim_average", item.get("average_fps"),
                        file_name=relative_name, path=f"{block}.average_fps",
                        block=block, unit="fps",
                    ),
                ],
                unknowns=["gg_fps_claim_average"],
                limitations=[
                    "an unspecified reported FPS is not an average and does not"
                    " become a measurement",
                ],
                envelope=item,
            )

        binding = gg.get("fps_claims_binding")
        if binding is not None:
            builder.record(
                local_id="fps_claims_binding",
                record_kind="claim_binding_observation",
                source_id=gg.get("source_id"),
                fields=[_observation(
                    "gg_fps_claims_binding", binding, file_name=relative_name,
                    path="gg.fps_claims_binding", block="gg",
                )],
                envelope={"fps_claims_binding": binding},
            )

        builder.limitations.append(RESEARCH_MAPPING_NOTES[RESEARCH_SOURCE_SECTIONS_FORMAT_ID])
        records.extend(builder.records)
        diagnostic.extend(builder.diagnostic)
        all_limitations.extend(builder.limitations)

    return ResearchMapping(
        format_id=RESEARCH_SOURCE_SECTIONS_FORMAT_ID,
        sources=all_sources,
        source_revisions=all_revisions,
        records=records,
        diagnostic_record_ids=diagnostic,
        limitations=all_limitations,
    )


def _map_research_checks(
    payload: Mapping[str, Any],
    *,
    snapshot_id: str,
    relative_name: str,
    clock: Callable[[], str],
) -> ResearchMapping:
    """Historical evidence only: archive the bytes, create no product fact."""

    limitations = [RESEARCH_MAPPING_NOTES[RESEARCH_CHECKS_FORMAT_ID]]
    if isinstance(payload.get("extraction_sha256"), str):
        limitations.append(
            "the checks file names an extraction by hash; it never rewrites that"
            " extraction or turns a past match into a current check result"
        )
    if isinstance(payload.get("interpretation"), str):
        limitations.append(
            "the document's own interpretation states this was not independent"
            " accuracy evidence"
        )
    return ResearchMapping(
        format_id=RESEARCH_CHECKS_FORMAT_ID,
        sources=[],
        source_revisions=[],
        records=[],
        diagnostic_record_ids=[],
        limitations=limitations,
    )


def map_research_document(
    payload: Mapping[str, Any],
    *,
    format_id: str,
    snapshot_id: str,
    relative_name: str,
    clock: Callable[[], str] = utc_now,
) -> ResearchMapping:
    """Apply the admitted format's explicit mapping table to one document."""

    if format_id == RESEARCH_RESULT_FORMAT_ID:
        return _map_research_result(
            payload, snapshot_id=snapshot_id, relative_name=relative_name, clock=clock,
        )
    if format_id == RESEARCH_SOURCE_SECTIONS_FORMAT_ID:
        return _map_research_source_sections(
            payload, snapshot_id=snapshot_id, relative_name=relative_name, clock=clock,
        )
    if format_id == RESEARCH_CHECKS_FORMAT_ID:
        return _map_research_checks(
            payload, snapshot_id=snapshot_id, relative_name=relative_name, clock=clock,
        )
    raise ContractError(
        ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT,
        f"no research mapping table exists for {format_id!r}",
    )


class ResearchImporter:
    """Freeze one research original and archive or map it (IMP-T04).

    The source is a single file the caller names; the directory is never
    enumerated.  The original is read once before and once after the copy, and
    the managed copy is read back — a run that changed underneath is refused.
    """

    def __init__(
        self,
        repository: ProductRepository,
        files: FileService,
        *,
        annotations: Sequence[ResearchAnnotation] = (),
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.repository = repository
        self.files = files
        self.clock = clock
        self.annotations = list(annotations)
        self.annotation_by_format = {
            annotation.format_id: annotation for annotation in self.annotations
        }

    def freeze_document(
        self,
        path: str | Path,
        *,
        user_scope_id: str,
        idempotency_key: str,
        canonical_route: str = RESEARCH_IMPORT_ROUTE,
        hooks: PublishHooks | None = None,
    ) -> ResearchFrozenDocument:
        path = Path(path)
        if not path.is_file() or path.is_symlink():
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "a research import names one existing, regular file",
            )
        name = path.name
        try:
            before = path.read_bytes()
        except OSError as error:
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                f"{name} could not be read from the source path",
            ) from error
        fingerprint = _fingerprint(before)

        def before_commit() -> None:
            try:
                after = path.read_bytes()
            except OSError as error:
                raise ContractError(
                    ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                    f"{name} could not be re-read before the copy was committed",
                ) from error
            if _fingerprint(after) != fingerprint:
                raise ContractError(
                    ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                    f"{name} changed while the research original was being copied",
                )

        published = self.files.publish(
            kind="import",
            files={name: before},
            user_scope_id=user_scope_id,
            method="POST",
            canonical_route=canonical_route,
            key=idempotency_key,
            request_schema_version=PRODUCT_CONTRACT_VERSION,
            request_hash=_freeze_request_hash({name: before}),
            writer=None,
            hooks=PublishHooks(before_commit=before_commit),
        )
        operation = self.files.get_operation(published.operation_id)
        file_id = published.file_ids[0]
        managed = self.files.read_managed_file(file_id)
        if _fingerprint(managed) != fingerprint:
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                "the managed copy does not reproduce the frozen original bytes",
            )
        return ResearchFrozenDocument(
            relative_name=name,
            file_id=file_id,
            manifest=operation.manifest,
            sha256=hashlib.sha256(before).hexdigest(),
        )

    def publish(
        self,
        frozen: ResearchFrozenDocument,
        *,
        user_scope_id: str,
        idempotency_key: str | None = None,
        canonical_route: str = RESEARCH_IMPORT_ROUTE,
        created_by_task_id: str | None = None,
        hooks: PublishHooks | None = None,
    ) -> ResearchImportResult:
        data = self.files.read_managed_file(frozen.file_id)
        if hashlib.sha256(data).hexdigest() != frozen.sha256:
            raise ContractError(
                ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                "the frozen research original changed after it was frozen",
            )
        detected, reason = _detect_research_format(data)
        annotation = self.annotation_by_format.get(detected) if detected else None
        problems = research_annotation_problems(annotation) if annotation else []
        admitted = annotation is not None and not problems

        if detected is None:
            annotation_status = "missing"
            mapping_version = RESEARCH_ARCHIVE_MAPPING_VERSION
        elif admitted:
            annotation_status = "independent"
            mapping_version = detected
        elif annotation is not None and annotation.annotation_method == "model_recheck":
            annotation_status = "model_recheck"
            mapping_version = RESEARCH_ARCHIVE_MAPPING_VERSION
        elif annotation is not None and annotation.reviewer_is_session_model:
            annotation_status = "model_recheck"
            mapping_version = RESEARCH_ARCHIVE_MAPPING_VERSION
        else:
            annotation_status = "rejected"
            mapping_version = RESEARCH_ARCHIVE_MAPPING_VERSION

        fingerprint = import_fingerprint(frozen.manifest)
        context_hash = hashlib.sha256(canonical_json({
            "mapping_version": mapping_version,
            "rule_version": RESEARCH_RULE_VERSION,
            "annotation": None if annotation is None else {
                "fixture_id": annotation.fixture_id,
                "fixture_sha256": annotation.fixture_sha256,
                "annotation_method": annotation.annotation_method,
                "reviewer_is_session_model": annotation.reviewer_is_session_model,
            },
        })).hexdigest()
        snapshot_id = _snapshot_id(fingerprint, mapping_version, context_hash)

        existing = self._find_by_triple(fingerprint, mapping_version, context_hash)
        if existing is not None:
            return self._result(existing)

        limitations: list[str] = []
        if detected is None:
            limitations.append(reason)
        if annotation is None:
            if detected is not None:
                limitations.append(
                    f"{detected} has no independent annotation fixture; its bytes"
                    " are retained and no structured record is published"
                )
        elif problems:
            limitations.extend(f"annotation gate: {problem}" for problem in problems)
        elif detected is not None:
            limitations.append(RESEARCH_MAPPING_NOTES.get(detected, ""))

        mapping: ResearchMapping | None = None
        if admitted and detected is not None:
            try:
                payload = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ContractError(
                    ErrorCode.IMPORT_SNAPSHOT_INCONSISTENT,
                    "the frozen research original is no longer UTF-8 JSON",
                ) from error
            mapping = map_research_document(
                payload,
                format_id=detected,
                snapshot_id=snapshot_id,
                relative_name=frozen.relative_name,
                clock=self.clock,
            )
            limitations.extend(mapping.limitations)

        accepted = [
            record.id for record in (mapping.records if mapping else [])
            if record.id not in set(mapping.diagnostic_record_ids if mapping else [])
        ]
        diagnostic = list(mapping.diagnostic_record_ids) if mapping else []
        code = "published" if accepted else "retained_only"
        declaration = {
            "kind": "research_import",
            "rule_version": RESEARCH_RULE_VERSION,
            "import_snapshot_id": snapshot_id,
            "import_fingerprint": fingerprint,
            "mapping_version": mapping_version,
            "mapping_context_hash": context_hash,
            "origin": "research_json",
            "relative_name": frozen.relative_name,
            "format_id": detected,
            "annotation_status": annotation_status,
            "annotation": None if annotation is None else annotation.model_dump(mode="json"),
            "result_code": code,
            "accepted_record_ids": accepted,
            "diagnostic_record_ids": diagnostic,
            "limitations": [item for item in limitations if item],
            "manifest": frozen.manifest.model_dump(mode="json"),
            "frozen_file_ids": [frozen.file_id],
        }
        key = idempotency_key or f"research:{snapshot_id}"
        request_hash = hashlib.sha256(canonical_json({
            "import_fingerprint": fingerprint,
            "mapping_version": mapping_version,
            "mapping_context_hash": context_hash,
            "user_scope_id": user_scope_id,
        })).hexdigest()
        self.files.publish(
            kind="import",
            files={f"imports/{snapshot_id}/{DECLARATION_FILE}": canonical_json(declaration)},
            user_scope_id=user_scope_id,
            method="POST",
            canonical_route=canonical_route,
            key=key,
            request_schema_version=PRODUCT_CONTRACT_VERSION,
            request_hash=request_hash,
            writer=lambda uow, operation, managed: self._write(
                uow,
                managed=managed,
                snapshot_id=snapshot_id,
                fingerprint=fingerprint,
                mapping_version=mapping_version,
                context_hash=context_hash,
                mapping=mapping,
                detected=detected,
                limitations=limitations,
                created_by_task_id=created_by_task_id,
                frozen_file_id=frozen.file_id,
                manifest=frozen.manifest,
            ),
            hooks=hooks,
        )
        return self._result(snapshot_id)

    def _write(
        self,
        uow: UnitOfWork,
        *,
        managed: Sequence[Any],
        snapshot_id: str,
        fingerprint: str,
        mapping_version: str,
        context_hash: str,
        mapping: ResearchMapping | None,
        detected: str | None,
        limitations: Sequence[str],
        created_by_task_id: str | None,
        frozen_file_id: str,
        manifest: FileManifest,
    ) -> list[ObjectRef]:
        winner = uow.query(
            "SELECT import_snapshot_id FROM import_snapshots"
            " WHERE import_fingerprint=? AND mapping_version=? AND mapping_context_hash=?",
            (fingerprint, mapping_version, context_hash),
        )
        if winner:
            return self._refs(uow, winner[0]["import_snapshot_id"])
        parent = uow.query(
            "SELECT import_snapshot_id FROM import_snapshots WHERE import_fingerprint=?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (fingerprint,),
        )
        snapshot = ImportSnapshot(
            id=snapshot_id,
            schema_version="product-contract/0.4",
            created_at=self.clock(),
            created_by_task_id=created_by_task_id,
            origin="research_json",
            legacy_run_id=None,
            manifest=snapshot_manifest(manifest),
            import_fingerprint=fingerprint,
            import_status=PublicationStatus.published,
            mapping_version=mapping_version,
            mapping_context_hash=context_hash,
            parent_import_snapshot_id=parent[0]["import_snapshot_id"] if parent else None,
            identity_mapping_ids=[],
            limitations=[item for item in limitations if item],
        )
        uow.insert_object("ImportSnapshot", snapshot)
        declaration_file_id = managed[0].id
        uow.execute(
            "INSERT INTO import_snapshots(import_snapshot_id, import_fingerprint,"
            " mapping_version, mapping_context_hash, import_status, declaration_file_id,"
            " parent_import_snapshot_id, mapped_game_id, created_by_task_id, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                fingerprint,
                mapping_version,
                context_hash,
                PublicationStatus.published.value,
                declaration_file_id,
                snapshot.parent_import_snapshot_id,
                None,
                created_by_task_id,
                self.clock(),
            ),
        )
        if mapping is not None:
            for source in mapping.sources:
                uow.insert_object("Source", source)
            for revision in mapping.source_revisions:
                uow.insert_object("SourceRevision", revision)
            diagnostic = set(mapping.diagnostic_record_ids)
            for record in mapping.records:
                uow.insert_object("ImportedRecord", record)
                uow.claim_imported_record_key(
                    record_snapshot_id=record.id,
                    import_snapshot_id=snapshot_id,
                    capture_scope_id=record.capture_scope_id,
                    record_type=record.record_type,
                    local_record_id=record.local_record_id,
                    usable=record.id not in diagnostic,
                )
            snapshot_ref = ObjectRef(entity_type="ImportSnapshot", entity_id=snapshot_id)
            for source in mapping.sources:
                uow.insert_reference(snapshot_id, "source", ObjectRef(
                    entity_type="Source", entity_id=source.id,
                ))
            for revision in mapping.source_revisions:
                uow.insert_reference(snapshot_id, "source_revision", ObjectRef(
                    entity_type="SourceRevision", entity_id=revision.id,
                ))
            for record in mapping.records:
                uow.insert_reference(snapshot_id, "record", ObjectRef(
                    entity_type="ImportedRecord", entity_id=record.id,
                ))
                uow.insert_reference(record.id, "source_revision", ObjectRef(
                    entity_type="SourceRevision", entity_id=record.source_revision_id,
                ))
                uow.insert_reference(record.id, "import_snapshot", snapshot_ref)
        uow.insert_reference(snapshot_id, "frozen_file", ObjectRef(
            entity_type="ManagedFile", entity_id=frozen_file_id,
        ))
        related = [
            ObjectRef(entity_type="ManagedFile", entity_id=declaration_file_id),
            ObjectRef(entity_type="ManagedFile", entity_id=frozen_file_id),
        ]
        uow.record_domain_event(
            target_type="ImportSnapshot",
            target_id=snapshot_id,
            dimension="publication",
            sequence=1,
            previous_state=None,
            next_state=PublicationStatus.published.value,
            cause="research_import_archived",
            related_refs=related,
            created_at=self.clock(),
        )
        return self._refs(uow, snapshot_id)

    def _result(self, snapshot_id: str) -> ResearchImportResult:
        with self.repository.unit_of_work() as uow:
            row = uow.query(
                "SELECT * FROM import_snapshots WHERE import_snapshot_id=?", (snapshot_id,)
            )
            if not row:
                raise ContractError(
                    ErrorCode.RESOURCE_NOT_FOUND, "the research import snapshot is not registered"
                )
            keys = uow.query(
                "SELECT record_snapshot_id, usable FROM imported_record_keys"
                " WHERE import_snapshot_id=? ORDER BY rowid",
                (snapshot_id,),
            )
            declaration_file_id = row[0]["declaration_file_id"]
        declaration = self.files.read_managed_file(declaration_file_id)
        declaration_payload = json.loads(declaration.decode("utf-8"))
        stored = self.repository.get_object("ImportSnapshot", snapshot_id)
        snapshot = ImportSnapshot.model_validate_json(stored.payload_bytes)
        accepted = [item["record_snapshot_id"] for item in keys if item["usable"]]
        diagnostic = [item["record_snapshot_id"] for item in keys if not item["usable"]]
        return ResearchImportResult(
            snapshot_id=snapshot_id,
            code="published" if accepted else "retained_only",
            format_id=declaration_payload.get("format_id"),
            annotation_status=declaration_payload.get("annotation_status", "missing"),
            accepted_record_ids=accepted,
            diagnostic_record_ids=diagnostic,
            limitations=list(snapshot.limitations),
            refs=[
                ObjectRef(entity_type="ImportSnapshot", entity_id=snapshot_id),
                ObjectRef(entity_type="ManagedFile", entity_id=declaration_file_id),
            ],
        )

    def _find_by_triple(
        self, fingerprint: str, mapping_version: str, context_hash: str
    ) -> str | None:
        with self.repository.unit_of_work() as uow:
            rows = uow.query(
                "SELECT import_snapshot_id FROM import_snapshots WHERE import_fingerprint=?"
                " AND mapping_version=? AND mapping_context_hash=?",
                (fingerprint, mapping_version, context_hash),
            )
        return rows[0]["import_snapshot_id"] if rows else None

    def _refs(self, uow: UnitOfWork, snapshot_id: str) -> list[ObjectRef]:
        row = uow.query(
            "SELECT declaration_file_id FROM import_snapshots WHERE import_snapshot_id=?",
            (snapshot_id,),
        )
        refs = [ObjectRef(entity_type="ImportSnapshot", entity_id=snapshot_id)]
        if row:
            refs.append(ObjectRef(
                entity_type="ManagedFile", entity_id=row[0]["declaration_file_id"]
            ))
        return refs


class ImportService:
    """Task handler for ``knowledge_import``: frozen ids in, result out."""

    def __init__(
        self,
        repository: ProductRepository,
        files: FileService,
        *,
        publisher: ImportPublisher | None = None,
        registry: FormatRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.files = files
        self.registry = registry or default_format_registry()
        self.publisher = publisher or ImportPublisher(
            repository, files, registry=self.registry
        )

    def handle(self, context: Any) -> Any:
        from game_app.contracts.tasks import DomainResult, KnowledgeImportInput
        from game_app.tasks import HandlerOutcome

        task_input = context.task_input
        if not isinstance(task_input, KnowledgeImportInput):
            raise ContractError(
                ErrorCode.INPUT_VALIDATION_FAILED,
                "the knowledge import handler received another task type",
            )
        context.phase("extract")
        frozen = frozen_group_from_file_ids(
            self.files, task_input.file_ids, self.registry
        )
        context.phase("validate")
        result = self.publisher.publish(
            frozen=frozen,
            game_context_id=task_input.game_context_id,
            mapping_version=task_input.mapping_version,
            identity_mapping_ids=task_input.identity_mapping_ids,
            user_scope_id=context.user_scope_id,
            created_by_task_id=context.task_id,
        )
        checks = [CheckResult(
            check_id="import.publish",
            definition_version=IMPORT_RULE_VERSION,
            status=(
                CheckStatus.fail if result.status is PublicationStatus.rejected
                else CheckStatus.pass_
            ),
            input_refs=[],
            evidence_refs=[],
            checked_at=self.publisher.clock(),
            method="deterministic import rule",
            note=f"import status {result.status.value}; result code {result.code}",
        )]
        published = result.code == "published"
        return HandlerOutcome(
            state=TaskState.completed if published else TaskState.partial,
            domain_result=DomainResult(
                domain=TaskType.knowledge_import,
                code=result.code,
                input_revision=context.task_input_revision,
                checks=checks,
            ),
            result_refs=list(result.refs),
        )


__all__ = [
    "COLLECTOR_DERIVED_FILES",
    "COLLECTOR_DIAGNOSTIC_FILES",
    "COLLECTOR_REQUIRED_FILES",
    "COLLECTOR_RECORD_GROUPS",
    "COLLECTOR_RUN_CONTRACT_VERSION",
    "COLLECTOR_RUN_FORMAT_ID",
    "FormatInspection",
    "FormatRegistry",
    "FormatSpec",
    "FreezeHooks",
    "FrozenGroup",
    "FrozenInputReader",
    "IMPORT_PUBLISH_ROUTE",
    "IMPORT_RULE_VERSION",
    "ImportPublishResult",
    "ImportPublisher",
    "ImportService",
    "ImportValidator",
    "ProbeResult",
    "RESEARCH_ARCHIVE_MAPPING_VERSION",
    "RESEARCH_CHECKS_FORMAT_ID",
    "RESEARCH_IMPORT_ROUTE",
    "RESEARCH_MAPPING_TABLES",
    "RESEARCH_RECORD_TYPE",
    "RESEARCH_RESULT_FORMAT_ID",
    "RESEARCH_RULE_VERSION",
    "RESEARCH_SOURCE_SECTIONS_FORMAT_ID",
    "ResearchAnnotation",
    "ResearchFieldMapping",
    "ResearchFrozenDocument",
    "ResearchImportResult",
    "ResearchImporter",
    "ResearchMapping",
    "ScopedImport",
    "ScopedReferenceMapper",
    "canonical_source_url",
    "default_format_registry",
    "frozen_group_from_file_ids",
    "import_fingerprint",
    "map_research_document",
    "mapping_context_hash",
    "read_frozen_bytes",
    "read_group",
    "record_payload_references",
    "research_annotation_problems",
    "research_format_registry",
    "research_format_specs",
    "snapshot_manifest",
    "verify_group_unchanged",
]
