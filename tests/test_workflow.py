import json
import sqlite3
from copy import deepcopy

from game_collector.providers import ProviderError
from game_collector.schema import Document
from game_collector.workflow import Collector
from tests.helpers import sample


class Search:
    name = "fake-search"

    def __init__(self):
        self.queries = []

    def search(self, query, limit):
        self.queries.append(query)
        return [{"url": "https://example.org/guide", "title": "Guide"}]


class Reader:
    name = "fake-reader"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def read(self, url):
        self.calls += 1
        if self.fail:
            raise ProviderError("blocked", retryable=True)
        doc, _ = sample()
        return doc.model_copy(update={"url": url, "revision_id": f"revision-{self.calls}"})


class Model:
    name = "fake-model"
    model = "synthetic-test-model"

    def __init__(self, gap=False, resolve=False, bad_resolution=False):
        self.calls = 0
        self.gap, self.resolve, self.bad_resolution = gap, resolve, bad_resolution

    def extract(self, doc, context):
        self.calls += 1
        _, x = sample()
        data = x.model_dump()
        if self.gap and self.calls == 1:
            data["gaps"] = [{"id": "gpu", "record_ids": [], "field": "gpu", "reason": "missing run hardware", "question": "Which GPU is explicitly named in the source run?", "priority": "P1", "query": "Example Game measured run GPU context"}]
        if self.resolve and context.get("pending_gaps"):
            if not self.bad_resolution:
                data["evidence"].append({"id": "answer", "kind": "observation", "origin": "source_statement", "statement": "RTX 3060", "proof": {"block_id": "b2", "quote": "RTX 3060", "scope": "run"}})
            data["resolutions"] = [{"gap_id": context["pending_gaps"][0]["id"], "field": "gpu", "answer": "RTX 3060", "scope": "run", "evidence_ids": ["missing" if self.bad_resolution else "answer"], "explanation": "RTX 3060 is stated for the cited run, not all configurations."}]
        return {"data": data, "raw": json.dumps(data), "model": self.model, "usage": {"prompt_tokens": 10}}


def request(**extra):
    return {"game_name": "Example Game", "mode": "seeded", "seed_urls": ["https://example.org/guide"], **extra}


def test_seeded_exports_sources_records_and_sqlite(tmp_path):
    result = Collector(Search(), Reader(), Model()).run(request(), tmp_path)
    assert result["status"] == "completed_scope"
    assert result["calls"]["read"] == 1 and result["calls"]["model"] == 1
    assert result["coverage"]["discovery_exercised"] is False
    records = json.loads((tmp_path / "records.json").read_text("utf-8"))
    assert all(r["record_status"] == "accepted" for r in records)
    assert (tmp_path / "report.md").exists()
    with sqlite3.connect(tmp_path / "knowledge.sqlite3") as db:
        assert db.execute("select count(*) from records").fetchone()[0] == 3


def test_discover_queries_diverse_roles_without_fps_prerequisite(tmp_path):
    search = Search()
    result = Collector(search, Reader(), Model()).run(request(mode="discover", seed_urls=[]), tmp_path)
    assert result["coverage"]["discovery_exercised"] is True
    assert len(search.queries) >= 3 and not any("60 fps" in q.lower() for q in search.queries)
    assert result["calls"]["read"] == 1


def test_followup_rereads_and_preserves_revisions_but_no_false_resolution(tmp_path):
    result = Collector(Search(), Reader(), Model(gap=True, resolve=True, bad_resolution=True)).run(request(), tmp_path)
    gaps = json.loads((tmp_path / "gaps.json").read_text("utf-8"))
    assert result["status"] == "partial_scope"
    assert gaps[0]["status"] == "stopped" and gaps[0]["attempts"]
    assert len(json.loads((tmp_path / "sources.json").read_text("utf-8"))) >= 2
    assert result["calls"]["read"] <= 3


def test_resolution_requires_accepted_new_answer_evidence(tmp_path):
    result = Collector(Search(), Reader(), Model(gap=True, resolve=True)).run(request(), tmp_path)
    gaps = json.loads((tmp_path / "gaps.json").read_text("utf-8"))
    assert gaps[0]["status"] == "resolved"
    assert gaps[0]["resolution_evidence_ids"]
    assert result["calls"]["read"] == 2


def test_hard_budget_exports_partial_and_never_exceeds_calls(tmp_path):
    result = Collector(Search(), Reader(), Model()).run(request(seed_urls=["https://example.org/a", "https://example.org/b"], budget={"max_urls": 1}), tmp_path)
    assert result["status"] == "partial_scope" and result["stop_reason"] == "budget_exhausted"
    assert result["calls"]["read"] == 1
    assert (tmp_path / "sources.json").exists()


def test_failure_retry_is_bounded_and_read_only_is_explicit(tmp_path):
    reader = Reader(fail=True)
    result = Collector(Search(), reader, Model()).run(request(), tmp_path)
    assert reader.calls == 2 and result["status"] == "partial_scope"
    assert result["calls"].get("model", 0) == 0
    result = Collector(Search(), Reader(), None).run(request(), tmp_path / "read-only")
    assert result["status"] == "partial_scope"
    assert json.loads((tmp_path / "read-only" / "records.json").read_text("utf-8")) == []


def test_duplicate_content_is_related_not_independent_support(tmp_path):
    result = Collector(Search(), Reader(), Model()).run(request(seed_urls=["https://example.org/a", "https://example.net/b"]), tmp_path)
    sources = json.loads((tmp_path / "sources.json").read_text("utf-8"))
    assert sources[1]["duplicate_of"] == sources[0]["id"]
    assert result["calls"]["model"] == 1


def test_model_failure_is_logged_not_empty_success(tmp_path):
    class FailedModel(Model):
        def extract(self, doc, context):
            raise ProviderError("model HTTP 401")
    result = Collector(Search(), Reader(), FailedModel()).run(request(), tmp_path)
    assert result["status"] == "partial_scope" and result["calls"]["model"] == 1
    assert any(c.get("error") == "model HTTP 401" for c in result["call_log"])


def test_malformed_model_json_preserved_and_format_only_repair(tmp_path):
    class FencedModel(Model):
        def extract(self, doc, context):
            result = super().extract(doc, context)
            result["raw"] = '```json\n' + result["raw"] + '\n```'
            result["data"] = None
            return result
    result = Collector(Search(), Reader(), FencedModel()).run(request(), tmp_path)
    assert result["status"] == "completed_scope"
    outputs = json.loads((tmp_path / "extractions.json").read_text("utf-8"))
    assert outputs[0]["format_repair"]["method"] == "strip_json_fence_only"
    assert outputs[0]["raw"].startswith("```json")


def test_output_directory_cannot_overwrite_previous_run(tmp_path):
    import pytest
    Collector(Search(), Reader(), Model()).run(request(), tmp_path)
    with pytest.raises(FileExistsError):
        Collector(Search(), Reader(), Model()).run(request(), tmp_path)


def test_single_missing_json_closer_repair_preserves_values(tmp_path):
    class BrokenJson(Model):
        def extract(self, doc, context):
            result = super().extract(doc, context)
            result["raw"] = result["raw"][:-1]
            result["data"] = None
            return result
    result = Collector(Search(), Reader(), BrokenJson()).run(request(), tmp_path)
    assert result["status"] == "completed_scope"
    output = json.loads((tmp_path / "extractions.json").read_text("utf-8"))[0]
    assert output["format_repair"]["method"] == "insert_single_missing_closer"
    assert output["format_repair"]["facts_changed"] is False


def test_one_bad_record_is_quarantined_without_discarding_valid_settings(tmp_path):
    class InvalidConfig(Model):
        def extract(self, doc, context):
            result = super().extract(doc, context)
            result["data"]["configurations"][0]["kind"] = "resolved_configuration"
            result["data"]["configurations"][0]["assignments"][0]["value"] = {"value_kind": "unknown", "value": None, "unknown_reason": "not_provided"}
            return result
    result = Collector(Search(), Reader(), InvalidConfig()).run(request(budget={"max_followup_rounds": 0}), tmp_path)
    rows = json.loads((tmp_path / "records.json").read_text("utf-8"))
    assert any(r["record_type"] == "settings" and r["record_status"] == "accepted" for r in rows)
    assert any(r["record_type"] == "configurations" and r["record_status"] == "quarantined" for r in rows)
    assert result["status"] == "partial_scope"


def test_unrelated_old_evidence_cannot_close_a_gpu_gap(tmp_path):
    class FalseResolver(Model):
        def extract(self, doc, context):
            result = super().extract(doc, context)
            if context.get("pending_gaps"):
                result["data"]["resolutions"] = [{"gap_id": context["pending_gaps"][0]["id"], "field": "gpu", "answer": "High", "scope": "run", "evidence_ids": ["rec"], "explanation": "answered"}]
            return result
    result = Collector(Search(), Reader(), FalseResolver(gap=True)).run(request(), tmp_path)
    gaps = json.loads((tmp_path / "gaps.json").read_text("utf-8"))
    assert gaps[0]["status"] != "resolved"
    assert result["status"] == "partial_scope"


def test_new_constraint_rechecks_old_configuration_and_keeps_history(tmp_path):
    from game_collector.schema import Block
    class UpdatedReader(Reader):
        def read(self, url):
            doc = super().read(url)
            doc.blocks.append(Block(id="b5", kind="paragraph", text="RTX 3060 does not support Shadows High."))
            return doc
    class ConstraintModel(Model):
        def extract(self, doc, context):
            result = super().extract(doc, context)
            d = result["data"]
            if self.calls == 1:
                d["gaps"][0].update(field="compatibility", record_ids=["cfg"], question="Does RTX 3060 support Shadows High?")
            else:
                d["evidence"].append({"id": "cap", "kind": "constraint", "origin": "source_statement", "statement": "RTX 3060 does not support Shadows High", "proof": {"block_id": "b5", "quote": "RTX 3060 does not support Shadows High", "scope": "section"}})
                d["constraints"] = [{"id": "rule", "evidence_id": "cap", "kind": "exclusion", "when": {"gpu": "RTX 3060"}, "setting_id": "shadows", "affected_values": ["High"], "verified_scope": True, "scope_note": "explicit source statement"}]
                gap = context["pending_gaps"][0]
                d["resolutions"] = [{"gap_id": gap["id"], "field": "compatibility", "answer": "RTX 3060 does not support Shadows High", "scope": "section", "evidence_ids": ["cap"], "explanation": "Explicit exclusion"}]
            return result
    result = Collector(Search(), UpdatedReader(), ConstraintModel(gap=True)).run(request(gpu="RTX 3060"), tmp_path)
    records = json.loads((tmp_path / "records.json").read_text("utf-8"))
    cfg = next(r for r in records if r["record_type"] == "configurations")
    assert cfg["recommendation_status"] == "excluded_branch"
    assert cfg["state_history"] and cfg["review_evidence_ids"]
    assert json.loads((tmp_path / "candidates.json").read_text("utf-8")) == []


def test_steering_wheel_guide_is_not_graphics_knowledge(tmp_path):
    class WheelReader(Reader):
        def read(self, url):
            from game_collector.reader import parse_html
            return parse_html(url, '<html><title>Forza Horizon 6 Fanatec wheel recommended settings</title><article><h1>Example Game wheel settings</h1><p>Steering wheel force feedback settings for Fanatec.</p></article></html>')
    model = Model()
    result = Collector(Search(), WheelReader(), model).run(request(budget={"max_followup_rounds": 0}), tmp_path)
    assert model.calls == 0
    sources = json.loads((tmp_path / "sources.json").read_text("utf-8"))
    assert sources[0]["status"] == "out_of_scope"


def test_optional_unknowns_do_not_prevent_declared_scope_completion(tmp_path):
    class OptionalGap(Model):
        def extract(self, doc, context):
            result = super().extract(doc, context)
            result["data"]["gaps"] = [{"id": "build", "field": "game_build", "priority": "P2", "reason": "not provided", "question": "Which build?"}]
            return result
    result = Collector(Search(), Reader(), OptionalGap()).run(request(budget={"max_followup_rounds": 0}), tmp_path)
    assert result["status"] == "completed_scope"


def test_one_reread_per_source_per_round_for_multiple_bad_records(tmp_path):
    class MultipleBad(Model):
        def extract(self, doc, context):
            result = super().extract(doc, context)
            result["data"]["settings"][0]["proof"]["quote"] = "not present"
            return result
    reader = Reader()
    Collector(Search(), reader, MultipleBad()).run(request(budget={"max_followup_rounds": 1}), tmp_path)
    assert reader.calls == 2


def test_incomplete_response_is_not_accepted_even_when_parseable(tmp_path):
    class Truncated(Model):
        def extract(self, doc, context):
            return {**super().extract(doc, context), "incomplete_reason": "length"}
    result = Collector(Search(), Reader(), Truncated()).run(request(budget={"max_followup_rounds": 0}), tmp_path)
    assert result["status"] == "partial_scope"
    assert json.loads((tmp_path / "records.json").read_text("utf-8")) == []
    assert json.loads((tmp_path / "extractions.json").read_text("utf-8"))[0]["raw"]
