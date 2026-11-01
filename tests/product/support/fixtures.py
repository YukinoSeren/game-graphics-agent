"""Test-only fixture and evidence tooling (VAL-T01).

Nothing here is product domain storage: fixtures are frozen inputs with hashes,
labels map deterministically to legal UUIDs, and reports are append-only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Literal
from uuid import UUID, uuid5

from pydantic import field_validator

from game_app.contracts.primitives import Hash, NonNegativeInt, Revision, StrictModel, Timestamp

EVIDENCE_LEVELS = ("static_design", "offline_fixture", "integration", "local_effect")
CASE_STATUSES = ("passed", "failed", "not_run", "blocked")

_NAMESPACE = UUID("6f0f0b6e-2f7a-4c1e-9a55-0d5c2e4b8a11")


class FixtureIntegrityError(Exception):
    """The fixture cannot be trusted: missing file, wrong length or wrong hash."""


class ReportConflict(Exception):
    """A recorded (run_id, case_id) result must not be overwritten."""


class FileEntry(StrictModel):
    relative_name: str
    byte_length: NonNegativeInt
    sha256: Hash
    media_type: str


class FixtureManifest(StrictModel):
    fixture_id: str
    version: Revision
    schema_version: str
    files: list[FileEntry]
    annotation_method: str
    known_limitations: list[str]

    @field_validator("files")
    @classmethod
    def non_empty(cls, value: list[FileEntry]) -> list[FileEntry]:
        if not value:
            raise ValueError("a fixture manifest declares at least one file")
        return value


class CaseSpec(StrictModel):
    case_id: str
    requirement_ids: list[str]
    purpose: str
    initial_fixture: str
    input_refs: list[str]
    steps: list[str]
    expected_assertions: list[str]
    mock_components: list[str]
    real_environment: str | None


class CaseResult(StrictModel):
    case_id: str
    fixture_version: Revision
    run_id: str
    status: Literal["passed", "failed", "not_run", "blocked"]
    evidence_level: Literal["static_design", "offline_fixture", "integration", "local_effect"]
    observed_files: list[FileEntry]
    failure_notes: list[str]


class RunManifest(StrictModel):
    run_id: str
    started_at: Timestamp
    ended_at: Timestamp | None
    git_revision: str
    python_version: str
    dependency_versions: dict[str, str]
    model_requested: str | None
    model_returned: str | None
    prompt_version: str | None
    contract_versions: list[str]
    environment_note: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FixtureLoader:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def fixture_dir(self, fixture_id: str, version: int) -> Path:
        return self.root / fixture_id / f"v{version}"

    def load_fixture(self, fixture_id: str, version: int) -> FixtureManifest:
        directory = self.fixture_dir(fixture_id, version)
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise FixtureIntegrityError(
                f"fixture {fixture_id}@v{version} has no manifest at {manifest_path.name}"
            )
        try:
            manifest = FixtureManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise FixtureIntegrityError(f"fixture {fixture_id}@v{version} manifest is invalid") from error
        if manifest.fixture_id != fixture_id or manifest.version != version:
            raise FixtureIntegrityError(
                f"fixture manifest identity {manifest.fixture_id}@v{manifest.version}"
                f" does not match the requested {fixture_id}@v{version}"
            )
        for entry in manifest.files:
            path = directory / entry.relative_name
            if not path.is_file():
                raise FixtureIntegrityError(f"declared fixture file is missing: {entry.relative_name}")
            data = path.read_bytes()
            if len(data) != entry.byte_length:
                raise FixtureIntegrityError(
                    f"fixture file {entry.relative_name} is {len(data)} bytes,"
                    f" manifest says {entry.byte_length}"
                )
            if _sha256(data) != entry.sha256:
                raise FixtureIntegrityError(
                    f"fixture file {entry.relative_name} does not match its recorded sha256"
                )
        return manifest

    def uuid_for(self, fixture_id: str, label: str) -> str:
        """Deterministic, legal UUID for a logical fixture label."""
        if not fixture_id or not label:
            raise ValueError("fixture_id and label must not be empty")
        return uuid5(_NAMESPACE, f"{fixture_id}:{label}").hex

    def load_cases(self, path: str | Path) -> list[CaseSpec]:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("a case file is a JSON array of CaseSpec objects")
        return [CaseSpec.model_validate(item) for item in payload]

    def run_case(
        self,
        spec: CaseSpec,
        environment: RunManifest,
        runner: Callable[[CaseSpec], tuple[str, Iterable[Any], Iterable[str]]],
        *,
        evidence_level: str = "offline_fixture",
    ) -> CaseResult:
        fixture_id, _, version_text = spec.initial_fixture.partition("@")
        if not version_text.isdigit():
            raise ValueError("initial_fixture must use '<fixture_id>@<version>'")
        status, observed_files, failure_notes = runner(spec)
        return CaseResult(
            case_id=spec.case_id,
            fixture_version=int(version_text),
            run_id=environment.run_id,
            status=status,
            evidence_level=evidence_level,
            observed_files=[_as_file_entry(item) for item in observed_files],
            failure_notes=list(failure_notes),
        )

    def write_report(
        self,
        results: Iterable[CaseResult],
        environment: RunManifest,
        out_dir: str | Path,
    ) -> Path:
        """Append this run's results; a recorded case in the same run never changes."""
        destination = Path(out_dir) / f"run-{environment.run_id}.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        recorded = _recorded_case_ids(destination)
        lines: list[str] = []
        for result in results:
            if result.case_id in recorded:
                raise ReportConflict(
                    f"case {result.case_id} is already recorded for run {environment.run_id};"
                    " use a new run_id to re-run"
                )
            if result.run_id != environment.run_id:
                raise ReportConflict("a result's run_id must match the written run manifest")
            recorded.add(result.case_id)
            lines.append(json.dumps(
                {
                    "run": environment.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ))
        if not lines:
            return destination
        with destination.open("a", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line + "\n")
        return destination


def _recorded_case_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    recorded: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            recorded.add(json.loads(line)["result"]["case_id"])
    return recorded


def _as_file_entry(value: Any) -> FileEntry:
    if isinstance(value, FileEntry):
        return value
    return FileEntry.model_validate(value)


def load_results(out_dir: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(Path(out_dir).glob("run-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records
