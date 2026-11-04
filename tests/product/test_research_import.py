"""IMP-T04: research formats are mapped only behind an independent annotation.

The repository's four research documents state, in their own metadata, that
they were produced and re-checked by the same session model.  That is a
``model_recheck``, not an independent annotation, so no research mapper is
registered by default: each original is archived as ``retained_only`` and no
structured record is published.

The mapping rules themselves (explicit locations, interval bounds, HDR as a
separate observation, dot/gg namespace separation, historical checks) are
exercised through :func:`map_research_document` and through the one synthetic
fixture that carries a complete, independent annotation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from game_app.contracts.knowledge import ImportedRecord, ImportSnapshot
from game_app.files import FileService
from game_app.importer import (
    RESEARCH_CHECKS_FORMAT_ID,
    RESEARCH_RESULT_FORMAT_ID,
    RESEARCH_SOURCE_SECTIONS_FORMAT_ID,
    ResearchAnnotation,
    ResearchImporter,
    default_format_registry,
    map_research_document,
    research_format_registry,
    research_format_specs,
    research_annotation_problems,
)
from game_app.repository import ProductRepository
from tests.product.support.validation import PRODUCT_FIXTURES
from tests.product.support.fixtures import FixtureLoader

SCOPE = "a" * 32
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = PRODUCT_FIXTURES / "research" / "v1"


# -- helpers ------------------------------------------------------------------


def _setup(tmp_path):
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    files = FileService(repository, tmp_path / "managed")
    return repository, files


def _copy_fixture(tmp_path, name: str) -> Path:
    FixtureLoader(PRODUCT_FIXTURES).load_fixture("research", 1)
    destination = tmp_path / name
    destination.write_bytes((FIXTURE_ROOT / name).read_bytes())
    return destination


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count(repository, entity_type: str) -> int:
    with repository.unit_of_work() as uow:
        return len(uow.iter_objects(entity_type))


def _table_count(repository, table: str) -> int:
    with repository.unit_of_work() as uow:
        return uow.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def _import(tmp_path, name: str, *, annotations=()):
    repository, files = _setup(tmp_path)
    source = _copy_fixture(tmp_path, name)
    importer = ResearchImporter(repository, files, annotations=annotations)
    frozen = importer.freeze_document(
        source, user_scope_id=SCOPE, idempotency_key=f"freeze:{name}"
    )
    result = importer.publish(frozen, user_scope_id=SCOPE)
    return repository, files, importer, frozen, result, source


def _annotation(
    *,
    format_id=RESEARCH_RESULT_FORMAT_ID,
    source_file="fh6-extraction-results.json",
    fixture_sha256,
    method="independent",
    session_model=False,
    coverage=None,
):
    return ResearchAnnotation(
        format_id=format_id,
        source_file=source_file,
        fixture_id="research-fixture",
        fixture_sha256=fixture_sha256,
        annotation_method=method,
        reviewer_source="synthetic-test-reviewer",
        reviewer_is_session_model=session_model,
        field_coverage=list(coverage or []),
    )


def _synthetic_result() -> dict:
    return {
        "record_version": "1.0",
        "run": {"date": "2026-11-01", "purpose": "synthetic mapping fixture"},
        "sources": [{
            "id": "SYN",
            "url": "https://example.invalid/synthetic",
            "author": "Synthetic Author",
            "published_date": "2026-05-20",
            "retrieved_date": "2026-11-01",
            "locators": {"recommendation": "synthetic table"},
        }],
        "recommendation": {
            "id": "SYN_REC",
            "source_id": "SYN",
            "type": "author_recommendation",
            "configuration_kind": "conditional_template",
            "locator_key": "recommendation",
            "fixed_settings": [{"row": 1, "setting": "Shadow Quality", "value": "High"}],
            "conditional_policies": [{
                "id": "upscaling", "policy": "choose one",
                "selected_value": None,
            }],
            "missing_fields": [],
            "measured_average_fps": None,
        },
        "ts_environments": [{
            "id": "SYN_ENV", "source_id": "SYN", "scope": "main",
            "gpu": "Synthetic GPU", "vram_gb": None, "output_resolution_label": "1080p",
        }],
        "wc_hardware": {},
        "wc_contexts": [],
        "configurations": [{
            "id": "SYN_CFG", "source_id": "SYN", "kind": "preset_reference",
            "label": "High", "expanded_settings": None,
        }],
        "wc_result_environments": [],
        "measurements": [{
            "id": "SYN_M1", "source_id": "SYN", "locator_key": "recommendation",
            "source_row": "High", "config_id": "SYN_CFG", "environment_id": "SYN_ENV",
            "average_fps": 120, "one_percent_low": 90, "point_one_percent_low": 70,
        }],
        "measurement_semantics": {
            "origin": "source_author_measurement",
            "independently_reproduced": False,
            "raw_logs_obtained": False,
            "low_metric_calculation_definition": None,
        },
        "comparisons": [],
        "exclusions": {},
    }


def _synthetic_annotation(document_bytes: bytes, name: str, **overrides):
    from game_app.importer import RESEARCH_MAPPING_TABLES

    coverage = sorted({
        row.product_field for row in RESEARCH_MAPPING_TABLES[RESEARCH_RESULT_FORMAT_ID]
    })
    return _annotation(
        source_file=name,
        fixture_sha256=hashlib.sha256(document_bytes).hexdigest(),
        coverage=coverage,
        **overrides,
    )


# -- the gate ----------------------------------------------------------------


def test_default_registry_has_no_research_mapper():
    registry = default_format_registry()
    assert registry.format_ids == ("collector-run/v0.1",)
    assert RESEARCH_RESULT_FORMAT_ID not in registry.format_ids
    assert RESEARCH_SOURCE_SECTIONS_FORMAT_ID not in registry.format_ids
    assert RESEARCH_CHECKS_FORMAT_ID not in registry.format_ids


def test_a_model_recheck_does_not_enable_a_mapper():
    sha = _sha(REPO_ROOT / "fh6-extraction-results.json")
    annotation = _annotation(
        fixture_sha256=sha, method="model_recheck", session_model=True,
        coverage=["source_url"],
    )
    assert research_annotation_problems(annotation)
    assert research_format_specs([annotation]) == []
    assert RESEARCH_RESULT_FORMAT_ID not in research_format_registry([annotation]).format_ids


def test_a_claimed_independent_label_from_the_session_model_is_still_a_recheck():
    sha = _sha(REPO_ROOT / "fh6-extraction-results.json")
    annotation = _annotation(
        fixture_sha256=sha, method="independent", session_model=True, coverage=["source_url"],
    )
    assert any("session" in problem for problem in research_annotation_problems(annotation))
    assert research_format_specs([annotation]) == []


def test_a_complete_independent_annotation_opens_the_gate_for_its_fixture():
    document = json.dumps(_synthetic_result(), ensure_ascii=False).encode("utf-8")
    annotation = _synthetic_annotation(document, "synthetic-research-result.json")
    assert research_annotation_problems(annotation) == []
    specs = research_format_specs([annotation])
    assert [spec.format_id for spec in specs] == [RESEARCH_RESULT_FORMAT_ID]
    registry = research_format_registry([annotation])
    assert RESEARCH_RESULT_FORMAT_ID in registry.format_ids


def test_an_annotation_for_different_bytes_never_matches():
    document = json.dumps(_synthetic_result(), ensure_ascii=False).encode("utf-8")
    annotation = _synthetic_annotation(document, "synthetic-research-result.json")
    registry = research_format_registry([annotation])
    inspection = registry.inspect_files({
        "synthetic-research-result.json": document.replace(b"120", b"121"),
    })
    assert inspection.format_id is None
    assert any("annotated fixture" in item for item in inspection.limitations)


def test_an_annotation_for_another_fixture_never_maps_the_frozen_bytes(tmp_path):
    annotated = json.dumps(_synthetic_result(), ensure_ascii=False).encode("utf-8")
    changed = json.dumps(
        {**_synthetic_result(), "run": {"date": "2026-11-02", "purpose": "other"}},
        ensure_ascii=False,
    ).encode("utf-8")
    annotation = _synthetic_annotation(annotated, "synthetic-research-result.json")
    source = tmp_path / "synthetic-research-result.json"
    source.write_bytes(changed)

    repository, files = _setup(tmp_path)
    importer = ResearchImporter(repository, files, annotations=[annotation])
    frozen = importer.freeze_document(
        source, user_scope_id=SCOPE, idempotency_key="freeze:changed"
    )
    result = importer.publish(frozen, user_scope_id=SCOPE)

    assert result.code == "retained_only"
    assert result.annotation_status == "rejected"
    assert result.accepted_record_ids == []
    assert _count(repository, "ImportedRecord") == 0
    assert any("fixture hash" in item for item in result.limitations)


# -- retained_only by default ------------------------------------------------


@pytest.mark.parametrize("name", [
    "fh6-extraction-results.json",
    "fh6-new-source-extraction.json",
    "fh6-extraction-checks.json",
    "fh6-new-source-checks.json",
])
def test_every_research_original_is_retained_only(tmp_path, name):
    repository, files, _importer, frozen, result, source = _import(tmp_path, name)

    assert result.code == "retained_only"
    assert result.accepted_record_ids == []
    assert result.annotation_status in {"missing", "model_recheck", "rejected"}
    assert _count(repository, "ImportedRecord") == 0
    assert _count(repository, "ImportSnapshot") == 1
    assert _table_count(repository, "external_calls") == 0

    stored = repository.get_object("ImportSnapshot", result.snapshot_id)
    snapshot = ImportSnapshot.model_validate_json(stored.payload_bytes)
    assert snapshot.origin == "research_json"
    entry = snapshot.manifest.entries[0]
    assert entry.sha256 == frozen.sha256
    assert files.read_managed_file(frozen.file_id) == source.read_bytes()
    assert _sha(source) == frozen.sha256


def test_the_repository_original_is_never_rewritten(tmp_path):
    original = REPO_ROOT / "fh6-new-source-extraction.json"
    before = _sha(original)
    _repository, _files, _importer, _frozen, result, _copy = _import(
        tmp_path, "fh6-new-source-extraction.json"
    )
    assert result.code == "retained_only"
    assert _sha(original) == before


def test_an_admitted_mapper_publishes_only_research_observations(tmp_path):
    document = json.dumps(_synthetic_result(), ensure_ascii=False).encode("utf-8")
    source = tmp_path / "synthetic-research-result.json"
    source.write_bytes(document)
    annotation = _synthetic_annotation(document, source.name)

    repository, files = _setup(tmp_path)
    importer = ResearchImporter(repository, files, annotations=[annotation])
    frozen = importer.freeze_document(
        source, user_scope_id=SCOPE, idempotency_key="freeze:synthetic"
    )
    result = importer.publish(frozen, user_scope_id=SCOPE)

    assert result.code == "published"
    assert result.annotation_status == "independent"
    assert result.accepted_record_ids
    records = [
        ImportedRecord.model_validate_json(
            repository.get_object("ImportedRecord", record_id).payload_bytes
        )
        for record_id in result.accepted_record_ids
    ]
    assert {record.record_type for record in records} == {"research_observation"}
    for record in records:
        assert record.payload["fields"]
        for field in record.payload["fields"]:
            assert field["source_location"]["file"] == source.name
            assert field["source_location"]["path"]
            assert field["source_location"]["block"]

    # A research observation is not a local measurement or a recommendation.
    for entity_type in (
        "PerformanceSummary", "RecommendationAction", "AppliedConfiguration", "Evidence",
        "Comparison", "RecommendationPackage",
    ):
        assert _count(repository, entity_type) == 0
    assert _table_count(repository, "external_calls") == 0


def test_research_import_is_idempotent_for_the_same_bytes(tmp_path):
    repository, files = _setup(tmp_path)
    source = _copy_fixture(tmp_path, "fh6-extraction-results.json")
    importer = ResearchImporter(repository, files)
    frozen = importer.freeze_document(
        source, user_scope_id=SCOPE, idempotency_key="freeze:same"
    )
    first = importer.publish(frozen, user_scope_id=SCOPE, idempotency_key="publish:one")
    second = importer.publish(frozen, user_scope_id=SCOPE, idempotency_key="publish:two")

    assert first.snapshot_id == second.snapshot_id
    assert _count(repository, "ImportSnapshot") == 1


# -- the mapping rules --------------------------------------------------------


def _map(name: str, format_id: str):
    payload = json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    return map_research_document(
        payload, format_id=format_id, snapshot_id="a" * 32, relative_name=name,
    )


def _record(mapping, kind: str) -> ImportedRecord:
    return next(
        record for record in mapping.records if record.payload["record_kind"] == kind
    )


def _field(record: ImportedRecord, name: str):
    return next(
        field for field in record.payload["fields"] if field["product_field"] == name
    )


def test_an_fps_interval_keeps_both_bounds_and_unknown_strictness():
    mapping = _map("fh6-new-source-extraction.json", RESEARCH_SOURCE_SECTIONS_FORMAT_ID)
    record = _record(mapping, "fps_interval")

    assert _field(record, "fps_lower")["value"] == 70
    assert _field(record, "fps_upper")["value"] == 85
    assert _field(record, "fps_average")["value"] is None  # 77.5 is never computed
    assert _field(record, "fps_aggregation_method")["value"] is None
    assert _field(record, "fps_lower")["boundary_semantics"] == "unknown"
    assert _field(record, "fps_upper")["boundary_semantics"] == "unknown"
    assert record.extension.boundary_semantics is not None
    assert record.extension.boundary_semantics.lower_inclusive is None
    assert record.extension.boundary_semantics.upper_inclusive is None
    assert record.extension.boundary_semantics.status.value == "unknown"


def test_hdr_is_a_separate_observation_and_not_a_graphics_value():
    mapping = _map("fh6-new-source-extraction.json", RESEARCH_SOURCE_SECTIONS_FORMAT_ID)
    hdr = _record(mapping, "hdr_availability_observation")
    configuration = _record(mapping, "source_configuration_section")

    assert _field(hdr, "hdr_reported_state")["value"]
    assert _field(hdr, "hdr_recommended_switch")["value"] is None
    configuration_fields = {
        field["product_field"] for field in configuration.payload["fields"]
    }
    assert "hdr_reported_state" not in configuration_fields
    assert not any(
        "hdr" in field["product_field"].lower()
        for field in configuration.payload["fields"]
    )


def test_dot_and_gg_local_ids_are_separate_namespaces():
    payload = {
        "run": {"date": "2026-11-01"},
        "sources": [
            {"id": "DOT", "url": "https://example.invalid/dot"},
            {"id": "GG", "url": "https://example.invalid/gg"},
        ],
        "dot": {
            "configuration_id": "SAME",
            "source_id": "DOT",
            "graphics_settings": [{"setting": "Shadow", "value": "High"}],
        },
        "gg": {
            "configuration_id": "SAME",
            "source_id": "GG",
            "matrix_rows": [{"setting": "Shadow", "values": ["High"]}],
        },
    }
    mapping = map_research_document(
        payload, format_id=RESEARCH_SOURCE_SECTIONS_FORMAT_ID,
        snapshot_id="b" * 32, relative_name="crafted.json",
    )
    configurations = [
        record for record in mapping.records
        if record.payload["record_kind"] in (
            "source_configuration_section", "tier_recommendation_matrix",
        )
    ]
    assert len(configurations) == 2
    assert configurations[0].id != configurations[1].id
    assert {record.capture_scope_id for record in configurations} == {
        f"{RESEARCH_SOURCE_SECTIONS_FORMAT_ID}:dot",
        f"{RESEARCH_SOURCE_SECTIONS_FORMAT_ID}:gg",
    }
    assert {record.payload["section"] for record in configurations} == {"dot", "gg"}


def test_a_bad_reference_isolates_only_that_observation():
    payload = _synthetic_result()
    payload["measurements"][0]["config_id"] = "MISSING_CONFIG"
    mapping = map_research_document(
        payload, format_id=RESEARCH_RESULT_FORMAT_ID,
        snapshot_id="c" * 32, relative_name="synthetic.json",
    )
    measurement = _record(mapping, "source_measurement")
    assert measurement.id in mapping.diagnostic_record_ids
    assert measurement.upstream_status.value == "quarantined"
    assert any(
        check.check_id == "research.reference" and check.status.value == "fail"
        for check in measurement.checks
    )
    # The other observations in the same document survive.
    assert _record(mapping, "reported_configuration").id not in mapping.diagnostic_record_ids
    assert _record(mapping, "author_recommendation_observation").id not in (
        mapping.diagnostic_record_ids
    )
    assert len(mapping.records) > 1


def test_a_source_measurement_is_not_a_performance_summary():
    mapping = _map("fh6-extraction-results.json", RESEARCH_RESULT_FORMAT_ID)
    measurement = _record(mapping, "source_measurement")
    assert _field(measurement, "measurement_origin")["value"] == "source_author_measurement"
    assert _field(measurement, "measurement_independently_reproduced")["value"] is False
    assert measurement.record_type == "research_observation"


def test_checks_are_historical_evidence_without_new_knowledge():
    mapping = _map("fh6-extraction-checks.json", RESEARCH_CHECKS_FORMAT_ID)
    assert mapping.records == []
    assert mapping.diagnostic_record_ids == []
    assert any("historical check evidence" in item for item in mapping.limitations)
    assert any("not independent" in item for item in mapping.limitations)


def test_a_review_summary_without_a_verified_mapping_stays_retained(tmp_path):
    repository, _files, _importer, _frozen, result, _source = _import(
        tmp_path, "fh6-new-source-checks.json"
    )
    assert result.format_id is None
    assert result.code == "retained_only"
    assert _count(repository, "ImportedRecord") == 0
    assert any("retained original" in item for item in result.limitations)


def test_every_mapping_table_row_names_a_product_field_and_a_location():
    from game_app.importer import RESEARCH_MAPPING_TABLES

    for format_id, rows in RESEARCH_MAPPING_TABLES.items():
        for row in rows:
            assert row.source_path, format_id
            assert row.product_field, format_id
            assert row.value_type, format_id
            assert row.location, format_id
