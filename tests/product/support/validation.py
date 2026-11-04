"""VAL-T02 case runner: records what ran, what did not, and exact counts.

The runner drives :class:`Application` directly (Q1 is unanswered, so there is
no HTTP).  It never writes to the product database beyond the case's own calls,
never touches ``runs/`` and never turns a skipped case into a pass.

``CaseResult`` rejects ``status=passed`` unless ``executed`` is true; a
``not_run``/``blocked`` result must carry its reason so the report cannot read
as all-green.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from uuid import uuid4

import pydantic
import pytest

from game_app.contracts.primitives import utc_now
from tests.product.support.fixtures import (
    CaseResult,
    CaseSpec,
    FixtureLoader,
    RunManifest,
    load_results,  # re-exported for report readers
)

PRODUCT_FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "product"
REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_LEVEL = "offline_fixture"


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def make_run_manifest(run_id: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        started_at=utc_now(),
        ended_at=None,
        git_revision=_git_revision(),
        python_version=platform.python_version(),
        dependency_versions={
            "pydantic": pydantic.VERSION,
            "pytest": pytest.__version__,
        },
        model_requested=None,
        model_returned=None,
        prompt_version=None,
        contract_versions=["product-contract/0.4"],
        environment_note=(
            "offline fixtures and temporary databases; no HTTP, no network,"
            " no real game, capture tool or model"
        ),
    )


class ScenarioRunner:
    """Run one case matrix against an Application and write an honest report."""

    def __init__(
        self,
        application: Any,
        repository: Any,
        *,
        run_id: str | None = None,
        evidence_level: str = EVIDENCE_LEVEL,
        fixture_root: str | Path = PRODUCT_FIXTURES,
    ) -> None:
        self.application = application
        self.repository = repository
        self.run_id = run_id or f"val-t02-{uuid4().hex[:12]}"
        self.evidence_level = evidence_level
        self.loader = FixtureLoader(fixture_root)
        self.environment = make_run_manifest(self.run_id)

    # -- exact counts -------------------------------------------------------
    def count(self, entity_type: str) -> int:
        with self.repository.unit_of_work() as uow:
            return len(uow.iter_objects(entity_type))

    def table_count(self, table: str) -> int:
        if not table.replace("_", "").isalnum():
            raise ValueError("a table name is an identifier")
        with self.repository.unit_of_work() as uow:
            return uow.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]

    def counts(self) -> dict[str, int]:
        return {
            "PlaySession": self.count("PlaySession"),
            "SessionSegment": self.count("SessionSegment"),
            "ConfigurationBinding": self.count("ConfigurationBinding"),
            "CaptureArtifact": self.count("CaptureArtifact"),
            "PerformanceSummary": self.count("PerformanceSummary"),
            "ComparisonProtocol": self.count("ComparisonProtocol"),
            "PerformanceComparison": self.count("PerformanceComparison"),
            "TestProtocol": self.count("TestProtocol"),
            "MetricDefinition": self.count("MetricDefinition"),
            "ManagedFile": self.count("ManagedFile"),
            "task_events": self.table_count("task_events"),
            "domain_events": self.table_count("domain_events"),
            "external_calls": self.table_count("external_calls"),
        }

    # -- cases --------------------------------------------------------------
    def run(
        self,
        *,
        case_id: str,
        requirement_ids: Sequence[str],
        purpose: str,
        steps: Sequence[str],
        expected_assertions: Sequence[str],
        execute: Callable[[Any], None],
        initial_fixture: str = "offline-log-basic@1",
        input_refs: Sequence[str] = (),
        mock_components: Sequence[str] = (),
        observations: Sequence[str] = (),
    ) -> CaseResult:
        spec = CaseSpec(
            case_id=case_id,
            requirement_ids=list(requirement_ids),
            purpose=purpose,
            initial_fixture=initial_fixture,
            input_refs=list(input_refs),
            steps=list(steps),
            expected_assertions=list(expected_assertions),
            mock_components=list(mock_components),
            real_environment=None,
        )

        def runner(_spec: CaseSpec) -> tuple[str, list[Any], list[str]]:
            try:
                execute(self.application)
            except Exception as error:  # noqa: BLE001 - a failed case is recorded
                return ("failed", [], [f"{type(error).__name__}: {error}"])
            return ("passed", [], [])

        result = self.loader.run_case(
            spec, self.environment, runner, evidence_level=self.evidence_level,
        )
        return result.model_copy(update={
            "observations": [
                *(f"{key}={value}" for key, value in self.counts().items()),
                *observations,
            ],
        })

    def not_run(
        self,
        *,
        case_id: str,
        requirement_ids: Sequence[str],
        reason: str,
        blocked: bool = False,
        blocked_by: str | None = None,
        initial_fixture: str = "offline-log-basic@1",
    ) -> CaseResult:
        return CaseResult(
            case_id=case_id,
            fixture_version=1,
            run_id=self.run_id,
            status="blocked" if blocked else "not_run",
            evidence_level=self.evidence_level,
            observed_files=[],
            failure_notes=[reason],
            executed=False,
            requirement_ids=list(requirement_ids),
            steps=[],
            expected_assertions=[],
            mock_components=[],
            observations=[],
            blocked_by=blocked_by if blocked else None,
        )

    def write_report(self, results: Iterable[CaseResult], out_dir: str | Path) -> Path:
        return self.loader.write_report(results, self.environment, out_dir)


class InsufficientEvidence(Exception):
    """A run whose every case was skipped or blocked is not a pass."""


def summarize_results(results: Iterable[CaseResult | dict[str, Any]]) -> dict[str, Any]:
    """Status counts plus the honest conclusion (08 §7/§10, VAL-V02/03)."""

    rows: list[CaseResult] = [
        result if isinstance(result, CaseResult) else CaseResult.model_validate(result)
        for result in results
    ]
    counts = {
        status: sum(1 for row in rows if row.status == status)
        for status in ("passed", "failed", "not_run", "blocked")
    }
    executed = sum(1 for row in rows if row.executed)
    gaps = [
        {
            "case_id": row.case_id,
            "status": row.status,
            "blocked_by": row.blocked_by,
            "reason": "; ".join(row.failure_notes),
        }
        for row in rows
        if row.status in ("not_run", "blocked")
    ]
    return {
        "total": len(rows),
        "executed": executed,
        **counts,
        "rows": [
            {
                "case_id": row.case_id,
                "status": row.status,
                "blocked_by": row.blocked_by,
            }
            for row in rows
        ],
        "gaps": gaps,
        "all_skipped": executed == 0,
        "conclusion": (
            "incomplete" if executed == 0 or counts["failed"]
            else "passed_with_gaps" if gaps else "passed"
        ),
    }


def conclude_results(results: Iterable[CaseResult | dict[str, Any]]) -> dict[str, Any]:
    """Refuse to call an all-skipped run a pass; otherwise return the summary."""

    summary = summarize_results(results)
    if summary["all_skipped"]:
        raise InsufficientEvidence(
            "no case executed in this run; skipped or blocked cases are not a pass"
        )
    return summary


__all__ = [
    "CaseResult",
    "CaseSpec",
    "EVIDENCE_LEVEL",
    "InsufficientEvidence",
    "PRODUCT_FIXTURES",
    "RunManifest",
    "ScenarioRunner",
    "conclude_results",
    "load_results",
    "make_run_manifest",
    "summarize_results",
]
