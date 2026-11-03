"""VAL-T02: the case matrix runs existing modules and records honest gaps.

Only B-1—B-3 modules are wired.  C/D/E cases are recorded as ``not_run`` with
their missing module, and one hardware case is ``blocked`` on Q2/G-* evidence.
The report is written to a temporary directory; nothing here is a product DB.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from game_app.application import Application
from game_app.contracts.performance import (
    CreateTestProtocolRequest,
    MetricCatalogQuery,
)
from game_app.contracts.primitives import BindingRef
from game_app.errors import ContractError, ErrorCode
from game_app.repository import canonical_json
from tests.product.support.configuration import context_key, setup
from tests.product.support.fixtures import CaseResult
from tests.product.support.performance import (
    analysis_input,
    artifact_from_csv,
    craft_artifact,
    csv_bytes,
    publish_log,
)
from tests.product.support.validation import ScenarioRunner, load_results
from tests.product.test_sessions import FakeClock


def _application(tmp_path):
    repository, files, installation, context, device = setup(tmp_path)
    clock = FakeClock()
    application = Application(
        repository, file_root=str(tmp_path / "managed"), clock=clock,
    )
    return repository, application, installation, context, device, clock


def _session(application, installation, context, device, key="session"):
    from game_app.contracts.performance import CreateSessionRequest

    request = CreateSessionRequest.model_validate_json(canonical_json({
        "installation_id": installation,
        "device_snapshot_id": device,
        "game_context_id": context,
        "applied_id": None,
        "configuration_state": "unverified",
        "run_declaration": {
            "installation_id": installation,
            "fields": {},
            "raw_text": "",
        },
        "test_protocol_id": None,
        "recommendation_id": None,
    }))
    result = application.create_session(request, context_key(key))
    session_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "PlaySession"
    )
    segment_id = next(
        ref.entity_id for ref in result.refs if ref.entity_type == "SessionSegment"
    )
    return session_id, segment_id


def test_an_unexecuted_case_cannot_be_recorded_as_passed():
    with pytest.raises(ValidationError):
        CaseResult(
            case_id="unexecuted",
            fixture_version=1,
            run_id="run-1",
            status="passed",
            evidence_level="offline_fixture",
            observed_files=[],
            failure_notes=[],
            executed=False,
        )
    honest = CaseResult(
        case_id="unexecuted",
        fixture_version=1,
        run_id="run-1",
        status="not_run",
        evidence_level="offline_fixture",
        observed_files=[],
        failure_notes=["no module"],
        executed=False,
    )
    assert honest.status == "not_run"


def test_the_matrix_runs_existing_modules_and_records_every_result(tmp_path):
    repository, application, installation, context, device, clock = _application(tmp_path)
    runner = ScenarioRunner(application, repository, run_id="run-contracts")

    def perf_v01(app):
        session_id, segment_id = _session(app, installation, context, device, "v01")
        file_id, _ = publish_log(app.files, csv_bytes([10, 20, 30, 40]), "v01-log")
        artifact = craft_artifact(
            repository, session_id=session_id, segment_id=segment_id,
            file_id=file_id, offset=0.0, scale=0.001,
        )
        with pytest.raises(ContractError) as caught:
            app.analyze(
                analysis_input(artifact.id, BindingRef(
                    segment_id=segment_id, segment_revision=1, binding_revision=1,
                )),
                context_key("v01-analysis"),
            )
        assert caught.value.code is ErrorCode.PRECONDITION_UNVERIFIED
        counts = runner.counts()
        assert counts["PerformanceSummary"] == 0
        assert counts["external_calls"] == 0
        with repository.unit_of_work() as uow:
            assert uow.query("SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0

    def perf_v02(app):
        artifact, binding = artifact_from_csv(
            app.performance, app.files, installation, context, device,
            csv_bytes([10, 20, 30, 40]), "v02-log",
        )
        accepted = app.analyze(analysis_input(artifact.id, binding), context_key("v02"))
        assert app.run_once().task_id == accepted.task_id
        view = app.get_task(accepted.task_id)
        assert view.task.state.value == "completed"
        summary = view.task.result_refs[0].entity_id
        from game_app.contracts.performance import PerformanceSummary
        from tests.product.support.performance import load_object

        stored = load_object(app.performance, "PerformanceSummary", summary, PerformanceSummary)
        average = next(
            item for item in stored.metrics
            if item.numeric.number_kind != "unknown" and item.unit == "fps"
        )
        assert average.numeric.value == 40.0
        assert runner.counts()["PerformanceSummary"] == 1
        assert runner.counts()["external_calls"] == 0

    def perf_v09(app):
        catalog = app.list_metric_definitions(
            MetricCatalogQuery(frame_stream_kind=None)
        )
        assert len(catalog.choices) == 4
        assert all(choice.definition_id is None for choice in catalog.choices)
        assert runner.counts()["MetricDefinition"] == 0
        request = CreateTestProtocolRequest.model_validate_json(canonical_json({
            "game_context_id": context,
            "parent_protocol_id": None,
            "scenario": "route",
            "condition_fields": {},
            "controlled_variables": [],
            "metric_registry_version": "project-metrics/1",
            "metric_keys": ["average_present_rate/v1"],
            "warmup_seconds": None,
            "sample_duration_seconds": None,
            "repeat_count": None,
            "note": None,
        }))
        first = app.create_test_protocol(request, context_key("v09-protocol"))
        replay = app.create_test_protocol(request, context_key("v09-protocol"))
        assert [ref.entity_id for ref in replay.refs] == [
            ref.entity_id for ref in first.refs
        ]
        with pytest.raises(ContractError) as caught:
            app.create_test_protocol(
                CreateTestProtocolRequest.model_validate_json(canonical_json({
                    "game_context_id": context,
                    "parent_protocol_id": first.refs[0].entity_id,
                    "scenario": "route",
                    "condition_fields": {},
                    "controlled_variables": [],
                    "metric_registry_version": "project-metrics/1",
                    "metric_keys": ["average_present_rate/v1"],
                    "warmup_seconds": None,
                    "sample_duration_seconds": None,
                    "repeat_count": None,
                    "note": None,
                })),
                context_key("v09-protocol"),
            )
        assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
        counts = runner.counts()
        assert counts["TestProtocol"] == 1
        assert counts["MetricDefinition"] == 1
        assert counts["external_calls"] == 0

    results = [
        runner.run(
            case_id="PERF-V09",
            requirement_ids=["D-P03", "PERF-V09"],
            purpose="the protocol entry is idempotent and writes no catalog GET",
            steps=["GET the metric catalog", "create a protocol", "replay the same key"],
            expected_assertions=[
                "four packaged choices with no definition id",
                "one TestProtocol and one MetricDefinition",
                "same key returns the same refs; different content is a 409",
            ],
            execute=perf_v09,
            initial_fixture="smoke-inputs@1",
            input_refs=["packaged project-metrics/1"],
            mock_components=["time stub"],
        ),
        runner.run(
            case_id="PERF-V01",
            requirement_ids=["D03", "PERF-V01"],
            purpose="an open segment/binding cannot be analyzed",
            steps=["create a session", "submit analyze with the open revision"],
            expected_assertions=[
                "PRECONDITION_UNVERIFIED",
                "PerformanceSummary count is 0",
                "task count is 0 and the key is not consumed",
                "external_calls is 0",
            ],
            execute=perf_v01,
            input_refs=["synthetic CSV"],
            mock_components=["time stub", "offline format registry"],
        ),
        runner.run(
            case_id="PERF-V02",
            requirement_ids=["D05", "PERF-V02"],
            purpose="the original formula on [10,20,30,40]",
            steps=["associate a synthetic log", "run analysis"],
            expected_assertions=[
                "average_present_rate == 40",
                "one PerformanceSummary",
                "external_calls is 0",
            ],
            execute=perf_v02,
            input_refs=["synthetic CSV"],
            mock_components=["time stub", "offline format registry"],
        ),
        runner.not_run(
            case_id="KNW-V01",
            requirement_ids=["A01", "KNW-V01"],
            reason="batch C (knowledge collection/mapping) is not implemented",
        ),
        runner.not_run(
            case_id="REC-V02",
            requirement_ids=["A08", "REC-V02"],
            reason="batch D (recommendation) is not implemented",
        ),
        runner.not_run(
            case_id="FBK-V01",
            requirement_ids=["A12", "FBK-V01"],
            reason="batch E (feedback/outcome) is not implemented",
        ),
        runner.not_run(
            case_id="PERF-V06-real",
            requirement_ids=["D02", "PERF-V06"],
            reason="needs a verified PresentMon adapter and a real Windows game"
                   " (Q2/G-GAME/G-FORMAT have no evidence)",
            blocked=True,
        ),
        runner.not_run(
            case_id="VAL-V02-full",
            requirement_ids=["VAL-V02"],
            reason="VAL-T02 is only the framework plus B-1—B-3; it is not closed",
        ),
    ]

    report = runner.write_report(results, tmp_path / "report")
    records = load_results(tmp_path / "report")

    assert report.is_file()
    assert len(records) == len(results)
    statuses = {record["result"]["case_id"]: record["result"]["status"] for record in records}
    assert statuses["PERF-V01"] == "passed"
    assert statuses["PERF-V02"] == "passed"
    assert statuses["PERF-V09"] == "passed"
    assert statuses["KNW-V01"] == "not_run"
    assert statuses["REC-V02"] == "not_run"
    assert statuses["FBK-V01"] == "not_run"
    assert statuses["PERF-V06-real"] == "blocked"
    assert statuses["VAL-V02-full"] == "not_run"
    assert any(status != "passed" for status in statuses.values())
    for record in records:
        result = record["result"]
        if result["status"] == "passed":
            assert result["executed"] is True
            assert result["requirement_ids"]
            assert result["steps"]
            assert result["expected_assertions"]
            assert result["mock_components"]
            assert result["observations"]
        else:
            assert result["failure_notes"]
        assert record["run"]["run_id"] == "run-contracts"
        assert record["run"]["git_revision"]
        assert "no HTTP" in record["run"]["environment_note"]
