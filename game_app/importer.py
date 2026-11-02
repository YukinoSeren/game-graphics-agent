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

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from game_app.contracts.primitives import StrictModel
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


__all__ = [
    "COLLECTOR_DERIVED_FILES",
    "COLLECTOR_DIAGNOSTIC_FILES",
    "COLLECTOR_REQUIRED_FILES",
    "COLLECTOR_RUN_CONTRACT_VERSION",
    "COLLECTOR_RUN_FORMAT_ID",
    "FormatInspection",
    "FormatRegistry",
    "FormatSpec",
    "FreezeHooks",
    "FrozenGroup",
    "FrozenInputReader",
    "ProbeResult",
    "default_format_registry",
    "read_group",
    "verify_group_unchanged",
]
