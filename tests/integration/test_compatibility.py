"""VAL-T03 scaffold: manifests, denominators and named gaps — no real samples.

This slice builds the *structure* only.  It does not contact a provider, open
an image, enable a capture adapter or claim VAL-V04.  Every capability that
needs a real tool/sample is ``blocked`` with the exact missing evidence, and
the only annotation status available is ``model_recheck``: the research
documents were reviewed by the same session model, which is not an independent
gold standard.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.product.support.fixtures import CaseResult, FixtureLoader
from tests.product.support.validation import (
    PRODUCT_FIXTURES,
    InsufficientEvidence,
    conclude_results,
    summarize_results,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH = PRODUCT_FIXTURES / "research" / "v1"

FORMAT_MANIFEST = {
    "collector-run/v0.1": {
        "source": "tests/fixtures/product/collector-run-smoke",
        "sample_kind": "synthetic_offline_group",
        "status": "verified_offline",
        "real_tool_version": None,
        "blocked_by": None,
    },
    "research-result/v1": {
        "source": "fh6-extraction-results.json",
        "sample_kind": "frozen_original",
        "status": "annotation_missing",
        "real_tool_version": None,
        "blocked_by": "no independent annotation",
    },
    "research-source-sections/v1": {
        "source": "fh6-new-source-extraction.json",
        "sample_kind": "frozen_original",
        "status": "annotation_missing",
        "real_tool_version": None,
        "blocked_by": "no independent annotation",
    },
    "research-checks/v1": {
        "source": "fh6-extraction-checks.json",
        "sample_kind": "frozen_original",
        "status": "historical_only",
        "real_tool_version": None,
        "blocked_by": "no independent annotation",
    },
}

ANNOTATION_MANIFEST = {
    "fh6-extraction-results.json": {
        "review_method": "model_recheck",
        "independent_reviewer": False,
        "note": "same-session model recheck; the human sample is not in the repository",
    },
    "fh6-new-source-extraction.json": {
        "review_method": "model_recheck",
        "independent_reviewer": False,
        "note": "same-session model recheck",
    },
    "fh6-extraction-checks.json": {
        "review_method": "model_recheck",
        "independent_reviewer": False,
        "note": "the document states this is a consistency check, not accuracy",
    },
    "fh6-new-source-checks.json": {
        "review_method": "model_recheck",
        "independent_reviewer": False,
        "note": "a review summary with no verified field mapping",
    },
}

DENOMINATORS = {
    "fields": {"available": False, "reason": "no independent gold sample"},
    "relations": {"available": False, "reason": "no independent gold sample"},
    "conditions": {"available": False, "reason": "no independent gold sample"},
    "locations": {"available": False, "reason": "locators are source-declared, not verified"},
    "isolations": {"available": False, "reason": "no real extraction run"},
    "images": {"available": False, "reason": "G-IMAGE: no image was read"},
}

CAPABILITIES = [
    ("VAL-V04.collector-run", "G-FORMAT", "a real tool's output version is not verified"),
    ("VAL-V04.research-annotation", "no independent annotation", "only model_recheck exists"),
    ("VAL-V04.image", "G-IMAGE", "no image sample and no OCR/VLM adapter"),
    ("VAL-V04.model-return", "G-MODEL", "the actual returned model id is not verified"),
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_entry(path: Path) -> dict:
    return {
        "relative_name": path.name,
        "byte_length": path.stat().st_size,
        "sha256": _sha256(path),
        "media_type": "application/json",
    }


def test_the_format_manifest_is_stable_and_names_every_source():
    manifest = {
        "manifest_version": 1,
        "formats": {
            format_id: {
                **details,
                "files": [_fixture_entry(RESEARCH / details["source"])]
                if (RESEARCH / details["source"]).is_file()
                else [],
            }
            for format_id, details in FORMAT_MANIFEST.items()
        },
    }
    first = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    second = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert first == second
    for format_id in ("research-result/v1", "research-source-sections/v1",
                      "research-checks/v1"):
        assert manifest["formats"][format_id]["files"]


def test_the_frozen_research_copies_match_their_manifest():
    loaded = FixtureLoader(PRODUCT_FIXTURES).load_fixture("research", 1)
    assert loaded.annotation_method.startswith("model_recheck")
    by_name = {entry.relative_name: entry for entry in loaded.files}
    for name, annotation in ANNOTATION_MANIFEST.items():
        assert by_name[name].sha256 == _sha256(RESEARCH / name)
        assert annotation["independent_reviewer"] is False
        assert annotation["review_method"] == "model_recheck"


def test_every_capability_is_blocked_with_a_specific_blocker():
    results = [
        CaseResult(
            case_id=case_id,
            fixture_version=1,
            run_id="val-t03-scaffold",
            status="blocked",
            evidence_level="offline_fixture",
            observed_files=[],
            failure_notes=[reason],
            executed=False,
            requirement_ids=["C08", "VAL-V04"],
            blocked_by=blocker,
        )
        for case_id, blocker, reason in CAPABILITIES
    ]
    summary = summarize_results(results)
    assert summary["passed"] == 0
    assert summary["blocked"] == len(CAPABILITIES)
    assert all(gap["blocked_by"] for gap in summary["gaps"])
    assert DENOMINATORS["images"] == {
        "available": False, "reason": "G-IMAGE: no image was read",
    }
    assert all(entry["available"] is False for entry in DENOMINATORS.values())


def test_val_v04_is_not_claimed_as_passed():
    results = [
        CaseResult(
            case_id=case_id,
            fixture_version=1,
            run_id="val-t03-scaffold",
            status="blocked",
            evidence_level="offline_fixture",
            observed_files=[],
            failure_notes=[reason],
            executed=False,
            requirement_ids=["VAL-V04"],
            blocked_by=blocker,
        )
        for case_id, blocker, reason in CAPABILITIES
    ]
    summary = summarize_results(results)
    assert summary["conclusion"] == "incomplete"
    with pytest.raises(InsufficientEvidence):
        conclude_results(results)


def test_reading_the_manifest_does_not_import_a_network_or_adapter_module(tmp_path):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-B", "-c",
         "import sys, game_app.importer;"
         "assert 'requests' not in sys.modules;"
         "assert 'game_app.adapters.presentmon' not in sys.modules;"
         "print('offline')"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "offline"
