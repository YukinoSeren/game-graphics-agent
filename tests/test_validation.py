from game_collector.schema import Extraction
from game_collector.validation import validate_extraction
from tests.helpers import sample


def check_changes(mutate):
    doc, extraction = sample()
    data = extraction.model_dump()
    mutate(data)
    return validate_extraction(Extraction.model_validate(data), doc, {"game_name": "Example Game"})


def by_id(rows, id):
    return next(r for r in rows if r["local_id"] == id)


def test_valid_source_is_not_automatically_applicable():
    doc, x = sample()
    cfg = by_id(validate_extraction(x, doc, {"game_name": "Example Game"}), "cfg")
    assert cfg["record_status"] == "accepted"
    assert cfg["applicability"] == "unknown" and cfg["recommendation_status"] == "needs_followup"


def test_missing_refs_and_fake_quotes_quarantine_dependents():
    rows = check_changes(lambda d: d["evidence"][0]["proof"].update(quote="Not in document"))
    assert by_id(rows, "rec")["record_status"] == "quarantined"
    assert by_id(rows, "cfg")["record_status"] == "quarantined"
    rows = check_changes(lambda d: d["configurations"][0]["assignments"][0].update(evidence_ids=["missing"]))
    assert by_id(rows, "cfg")["record_status"] == "quarantined"


def test_known_assignment_value_must_occur_in_source_cell():
    rows = check_changes(lambda d: d["configurations"][0]["assignments"][0]["value"].update(value="Ultra"))
    assert "assignment_value_not_grounded" in by_id(rows, "cfg")["issues"]


def test_cross_scene_comparison_and_numeric_invention_rejected():
    def mutate(d):
        for i, (block, fps, scene) in enumerate([("b2", 75, "Scene A"), ("b3", 90, "Scene B")]):
            proof = {"block_id": block, "quote": scene, "scope": "run"}
            d.setdefault("environments", []).append({"id": f"env{i}", "fields": {"scene": {"value": scene, "proof": proof}}})
            d["evidence"].append({"id": f"m{i}", "kind": "measurement", "origin": "source_author_measurement", "statement": "FPS", "proof": {**proof, "quote": f"average FPS {fps}"}, "environment_id": f"env{i}", "numeric": {"number_kind": "exact", "value": fps}, "metric": "average_fps", "unit": "FPS"})
        d["comparisons"] = [{"id": "cmp", "measurement_ids": ["m0", "m1"], "strictly_comparable": True, "rationale": "same scene", "formula": "b / a - 1"}]
    rows = check_changes(mutate)
    assert "comparison_conditions_differ_or_unknown" in by_id(rows, "cmp")["issues"]
    rows = check_changes(lambda d: d["evidence"].append({"id": "badnum", "kind": "measurement", "origin": "source_author_measurement", "statement": "FPS", "proof": {"block_id": "b2", "quote": "average FPS 75", "scope": "run"}, "numeric": {"number_kind": "exact", "value": 750}, "metric": "average_fps", "unit": "FPS"}))
    assert "numeric_not_grounded" in by_id(rows, "badnum")["issues"]


def test_interval_cannot_be_promoted_to_average_and_hdr_not_setting():
    def mutate(d):
        d["evidence"].append({"id": "obs", "kind": "observation", "origin": "source_statement", "statement": "driving", "proof": {"block_id": "b4", "quote": "70–85 FPS", "scope": "run"}, "numeric": {"number_kind": "interval", "lower": 70, "upper": 85}, "metric": "fps_unspecified", "unit": "FPS"})
    rows = check_changes(mutate)
    assert by_id(rows, "obs")["record_status"] == "accepted"
    assert by_id(rows, "obs")["payload"]["numeric"]["value"] is None


def test_constraints_need_evidence_and_only_affect_matching_branch():
    def mutate(d):
        p = {"block_id": "b4", "quote": "RTX 3060 does not support DLSS 4X", "scope": "section"}
        d["settings"][0].update(raw_name="DLSS 4X", proof={**p, "quote": "DLSS 4X"})
        d["configurations"][0]["assignments"][0]["value"] = {"value_kind": "alternatives", "alternatives": ["DLSS 4X", "High"]}
        d["configurations"][0]["assignments"][0]["evidence_ids"] = ["rec", "cap"]
        d["evidence"].append({"id": "cap", "kind": "constraint", "origin": "source_statement", "statement": p["quote"], "proof": p})
        d["constraints"] = [{"id": "rule", "evidence_id": "cap", "kind": "exclusion", "when": {"gpu": "RTX 3060"}, "setting_id": "shadows", "affected_values": ["DLSS 4X"], "verified_scope": True, "scope_note": "this source hardware"}]
    doc, x = sample()
    data = x.model_dump()
    mutate(data)
    rows = validate_extraction(Extraction.model_validate(data), doc, {"game_name": "Example Game", "gpu": "RTX 3060"})
    cfg = by_id(rows, "cfg")
    assert cfg["record_status"] == "accepted"
    assert cfg["branch_checks"][0]["status"] == "excluded_branch"
    assert cfg["recommendation_status"] == "needs_followup"


def test_wrong_table_coordinate_and_game_mismatch():
    rows = check_changes(lambda d: d["evidence"][0]["proof"].update(column=0))
    assert by_id(rows, "rec")["record_status"] == "quarantined"
    rows = check_changes(lambda d: d.update(game_match="mismatch"))
    assert all(r["record_status"] == "quarantined" for r in rows)


def test_configuration_header_column_cannot_take_another_gpu_column():
    from game_collector.reader import parse_html
    doc, x = sample()
    doc = parse_html(doc.url, '<article><h1>Example Game PC settings</h1><table><tr><th>Setting</th><th>RTX 3060</th><th>RTX 4090</th></tr><tr><td>Shadows</td><td>Low</td><td>Ultra</td></tr></table></article>')
    data = x.model_dump()
    data["settings"][0]["proof"] = {"block_id": "b1", "quote": "Shadows", "scope": "cell", "row": 1, "column": 0}
    data["configurations"][0]["proof"] = {"block_id": "b1", "quote": "RTX 3060", "scope": "cell", "row": 0, "column": 1}
    data["configurations"][0]["assignments"][0]["value"]["value"] = "Ultra"
    data["evidence"][0]["proof"] = {"block_id": "b1", "quote": "Ultra", "scope": "cell", "row": 1, "column": 2}
    rows = validate_extraction(Extraction.model_validate(data), doc, {"game_name": "Example Game"})
    assert "assignment_configuration_column_mismatch" in by_id(rows, "cfg")["issues"]


def test_constraint_predicate_and_target_must_be_grounded():
    def mutate(d):
        p = {"block_id": "b4", "quote": "RTX 3060 does not support DLSS 4X", "scope": "section"}
        d["evidence"].append({"id": "cap", "kind": "constraint", "origin": "source_statement", "statement": p["quote"], "proof": p})
        d["constraints"] = [{"id": "rule", "evidence_id": "cap", "kind": "exclusion", "when": {"gpu": "RTX 4090"}, "setting_id": "shadows", "affected_values": ["High"], "verified_scope": True, "scope_note": "invented mapping"}]
    doc, x = sample()
    data = x.model_dump()
    mutate(data)
    rows = validate_extraction(Extraction.model_validate(data), doc, {"game_name": "Example Game", "gpu": "RTX 4090"})
    assert by_id(rows, "rule")["record_status"] == "quarantined"
    assert by_id(rows, "cfg")["recommendation_status"] != "excluded_branch"


def test_setting_name_and_metric_unit_cannot_be_invented():
    rows = check_changes(lambda d: d["settings"][0].update(raw_name="Motion Blur"))
    assert by_id(rows, "shadows")["record_status"] == "quarantined"
    rows = check_changes(lambda d: d["evidence"].append({"id": "wrongunit", "kind": "measurement", "origin": "source_author_measurement", "statement": "latency", "proof": {"block_id": "b2", "quote": "average FPS 75", "scope": "run"}, "metric": "latency", "unit": "milliseconds", "numeric": {"number_kind": "exact", "value": 75}}))
    assert by_id(rows, "wrongunit")["record_status"] == "quarantined"


def test_row_quote_is_grounded_against_row_and_markdown_formatting():
    from game_collector.reader import parse_html
    from game_collector.schema import Proof
    from game_collector.validation import proof_issues
    doc = parse_html("https://example.org", '<article><table><tr><td>**Shadows**</td><td>High</td></tr></table></article>')
    proof = Proof(block_id="b0", quote="Shadows | High", scope="row", row=0, column=0)
    assert proof_issues(proof, doc) == []
