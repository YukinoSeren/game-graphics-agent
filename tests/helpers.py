from game_collector.reader import parse_html
from game_collector.schema import Extraction


def sample():
    doc = parse_html("https://example.org/guide", '''<article><h1>Example Game PC settings</h1>
    <table><tr><th>Setting</th><th>Value</th></tr><tr><td>Shadows</td><td>High</td></tr></table>
    <p>RTX 3060; Scene A; average FPS 75.</p><p>Scene B; average FPS 90.</p>
    <p>RTX 3060 does not support DLSS 4X. Reported driving 70–85 FPS.</p></article>''')
    proof = {"block_id": "b1", "quote": "High", "scope": "cell", "row": 1, "column": 1}
    data = {
        "game_match": "matched", "game_proof": {"block_id": "b0", "quote": "Example Game", "scope": "page"}, "platform": "Windows PC",
        "settings": [{"id": "shadows", "raw_name": "Shadows", "proof": {**proof, "quote": "Shadows", "column": 0}}],
        "configurations": [{"id": "cfg", "name": "High profile", "kind": "conditional_template", "proof": proof,
                            "assignments": [{"setting_id": "shadows", "value": {"value_kind": "fixed", "value": "High"}, "evidence_ids": ["rec"]}]}],
        "evidence": [{"id": "rec", "kind": "recommendation", "origin": "source_statement", "statement": "High shadows", "setting_ids": ["shadows"], "configuration_id": "cfg", "proof": proof}],
    }
    return doc, Extraction.model_validate(data)
