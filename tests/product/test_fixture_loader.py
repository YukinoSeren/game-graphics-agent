"""VAL-T01: frozen fixture loading, deterministic labels and append-only reports.

The loader is test tooling; it never rewrites golden inputs or earlier results.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.product.support.fixtures import (
    CaseResult,
    CaseSpec,
    FixtureIntegrityError,
    FixtureLoader,
    ReportConflict,
    RunManifest,
    load_results,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "product"


def _run_manifest(run_id: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        started_at="2026-11-02T00:00:00+00:00",
        ended_at="2026-11-02T00:00:05+00:00",
        git_revision="b" * 40,
        python_version="3.13.9",
        dependency_versions={"pydantic": "2.13.4", "pytest": "8.3.3"},
        model_requested=None,
        model_returned=None,
        prompt_version=None,
        contract_versions=["product-contract/0.4"],
        environment_note="offline fixture run; no provider or game",
    )


def _case(case_id: str = "VAL-T01-smoke") -> CaseSpec:
    return CaseSpec(
        case_id=case_id,
        requirement_ids=["VAL-V01"],
        purpose="verify fixture loading stays honest",
        initial_fixture="smoke-inputs@1",
        input_refs=["context"],
        steps=["load the fixture"],
        expected_assertions=["hash and byte length match the manifest"],
        mock_components=[],
        real_environment=None,
    )


def test_valid_fixture_loads_with_hash_and_length_checks():
    loader = FixtureLoader(FIXTURES)
    manifest = loader.load_fixture("smoke-inputs", 1)
    assert manifest.fixture_id == "smoke-inputs"
    assert manifest.files[0].relative_name == "inputs/context.json"


def test_corrupted_or_truncated_fixture_is_rejected(tmp_path):
    copied = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, copied)
    target = copied / "smoke-inputs" / "v1" / "inputs" / "context.json"
    target.write_bytes(target.read_bytes().replace(b'1', b'2'))
    loader = FixtureLoader(copied)
    with pytest.raises(FixtureIntegrityError):
        loader.load_fixture("smoke-inputs", 1)

    target.write_bytes(b"{}\n")
    with pytest.raises(FixtureIntegrityError):
        loader.load_fixture("smoke-inputs", 1)


def test_unknown_fixture_or_version_is_rejected(tmp_path):
    loader = FixtureLoader(FIXTURES)
    with pytest.raises(FixtureIntegrityError):
        loader.load_fixture("does-not-exist", 1)
    with pytest.raises(FixtureIntegrityError):
        loader.load_fixture("smoke-inputs", 9)


def test_label_mapping_is_stable_and_a_legal_uuid():
    loader = FixtureLoader(FIXTURES)
    first = loader.uuid_for("smoke-inputs", "installation-a")
    assert first == loader.uuid_for("smoke-inputs", "installation-a")
    assert len(first) == 32 and first == first.lower()
    int(first, 16)
    # Different fixtures and labels never collide.
    assert first != loader.uuid_for("smoke-inputs", "installation-b")
    assert first != loader.uuid_for("other-fixture", "installation-a")


def test_case_result_rejects_unknown_status_and_evidence_level():
    with pytest.raises(ValidationError):
        CaseResult(
            case_id="x",
            fixture_version=1,
            run_id="r",
            status="passed-ish",
            evidence_level="offline_fixture",
            observed_files=[],
            failure_notes=[],
        )
    with pytest.raises(ValidationError):
        CaseResult(
            case_id="x",
            fixture_version=1,
            run_id="r",
            status="passed",
            evidence_level="vibes",
            observed_files=[],
            failure_notes=[],
        )


def test_reports_append_new_runs_and_preserve_old_failures(tmp_path):
    loader = FixtureLoader(FIXTURES)
    failed = loader.run_case(
        _case("case-fail"),
        _run_manifest("run-1"),
        lambda spec: ("failed", [], ["expected hash mismatch"]),
    )
    first_path = loader.write_report([failed], _run_manifest("run-1"), tmp_path)
    first_bytes = first_path.read_bytes()

    passed = loader.run_case(
        _case("case-fail"),
        _run_manifest("run-2"),
        lambda spec: ("passed", [], []),
    )
    second_path = loader.write_report([passed], _run_manifest("run-2"), tmp_path)
    assert second_path != first_path
    assert first_path.read_bytes() == first_bytes

    results = load_results(tmp_path)
    assert [entry["result"]["run_id"] for entry in results] == ["run-1", "run-2"]
    assert [entry["result"]["status"] for entry in results] == ["failed", "passed"]


def test_same_run_cannot_overwrite_a_recorded_case(tmp_path):
    loader = FixtureLoader(FIXTURES)
    result = loader.run_case(
        _case(), _run_manifest("run-1"), lambda spec: ("not_run", [], ["blocked on hardware"])
    )
    loader.write_report([result], _run_manifest("run-1"), tmp_path)
    with pytest.raises(ReportConflict):
        loader.write_report([result], _run_manifest("run-1"), tmp_path)


def test_not_run_and_blocked_are_preserved_not_upgraded(tmp_path):
    loader = FixtureLoader(FIXTURES)
    not_run = loader.run_case(
        _case("case-not-run"), _run_manifest("run-1"), lambda spec: ("not_run", [], ["needs browser"])
    )
    blocked = loader.run_case(
        _case("case-blocked"), _run_manifest("run-1"), lambda spec: ("blocked", [], ["needs GPU"])
    )
    assert not_run.status == "not_run"
    assert blocked.status == "blocked"
    assert not_run.failure_notes == ["needs browser"]


def test_case_spec_requires_explicit_null_real_environment():
    payload = _case().model_dump(mode="json")
    payload.pop("real_environment")
    with pytest.raises(ValidationError):
        CaseSpec.model_validate(payload)
    # Explicit null is the valid representation of "no real environment".
    payload["real_environment"] = None
    assert CaseSpec.model_validate(payload).real_environment is None


def test_fixture_bytes_are_not_rewritten_by_loading():
    target = FIXTURES / "smoke-inputs" / "v1" / "inputs" / "context.json"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    FixtureLoader(FIXTURES).load_fixture("smoke-inputs", 1)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before


# --- review fix pass 2: a case cannot run against an unverified fixture ------


def test_run_case_refuses_a_missing_fixture(tmp_path):
    loader = FixtureLoader(FIXTURES)
    spec = _case("case-missing").model_copy(update={"initial_fixture": "no-such-fixture@7"})
    with pytest.raises(FixtureIntegrityError):
        loader.run_case(spec, _run_manifest("run-1"), lambda case: ("passed", [], []))


def test_run_case_refuses_a_corrupted_fixture(tmp_path):
    copied = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, copied)
    target = copied / "smoke-inputs" / "v1" / "inputs" / "context.json"
    target.write_bytes(b"{}\n")

    loader = FixtureLoader(copied)
    with pytest.raises(FixtureIntegrityError):
        loader.run_case(_case(), _run_manifest("run-1"), lambda case: ("passed", [], []))
