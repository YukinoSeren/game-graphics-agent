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
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from game_app.contracts.common import CheckResult
from game_app.contracts.enums import (
    CheckStatus,
    LegacyRecordState,
)
from game_app.contracts.knowledge import (
    BoundarySemantics,
    IdentityMapping,
    ImportedExtraction,
    ImportedRecord,
    ImportValidation,
    RecordExtension,
    Source,
    SourceMetadata,
    SourceRevision,
)
from game_app.contracts.primitives import StrictModel, new_id, utc_now
from game_app.errors import ContractError, ErrorCode
from game_app.files import FileManifest, FileService, PublishHooks
from game_app.repository import canonical_json

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
            content_hash=None,
            document_file_id=None,
            capture_method="collector_run_metadata",
            capture_scope=source_id,
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
                "the frozen group carries no source document; only its metadata was captured",
            )],
        )
        sources.append(source)
        revisions.append(revision)
        revision_by_source[(source_id, revision_id)] = revision.id
    return sources, revisions, revision_by_source


def _source_limitations(revisions: Sequence[SourceRevision]) -> list[str]:
    return [
        f"source revision {revision.capture_scope!r} has no managed document;"
        " its text cannot be re-read from this import"
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
    stored = files.repository.find_object("Game", target)
    if stored is None:
        return None, [
            f"the identity mapping for {legacy_value!r} targets a game that does not exist;"
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
    "IMPORT_RULE_VERSION",
    "ImportValidator",
    "ProbeResult",
    "ScopedImport",
    "ScopedReferenceMapper",
    "canonical_source_url",
    "default_format_registry",
    "read_frozen_bytes",
    "read_group",
    "verify_group_unchanged",
]
