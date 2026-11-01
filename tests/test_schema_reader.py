import pytest
from pydantic import ValidationError

from game_collector.schema import NumericValue, SettingValue, EnvironmentField
from game_collector.reader import canonical_url, parse_html


def test_interval_and_unknown_are_not_coerced():
    n = NumericValue(number_kind="interval", lower=70, upper=85)
    assert n.value is None and n.lower == 70
    with pytest.raises(ValidationError):
        NumericValue(number_kind="interval", lower=85, upper=70)
    with pytest.raises(ValidationError):
        NumericValue(number_kind="exact", value=True)
    with pytest.raises(ValidationError):
        SettingValue(value_kind="unknown", value="High", unknown_reason="not_provided")
    with pytest.raises(ValidationError):
        SettingValue(value_kind="unknown")


def test_conditional_and_alternatives_stay_unresolved():
    assert SettingValue(value_kind="alternatives", alternatives=["DLSS", "FSR"]).value is None
    c = SettingValue(value_kind="conditional", branches=[{"condition": "if supported", "value": "DLSS"}])
    assert c.value is None and len(c.branches) == 1
    with pytest.raises(ValidationError):
        SettingValue(value_kind="alternatives", alternatives=[])
    with pytest.raises(ValidationError):
        EnvironmentField(value=None)


def test_table_columns_and_sections_have_stable_locators():
    html = '''<html><head><title>Guide</title></head><body><nav>Noise</nav>
    <article><h1>Game</h1><h2>GPU tiers</h2><p>Choose a tier.</p>
    <table><tr><th>Setting</th><th>Budget</th><th>High end</th></tr>
    <tr><td>Shadows</td><td>Low</td><td>Ultra</td></tr></table>
    <img src="/benchmark.png" alt="FPS graph"><script>steal()</script></article></body></html>'''
    d = parse_html("https://example.org/guide", html)
    table = next(b for b in d.blocks if b.kind == "table")
    assert table.rows == [["Setting", "Budget", "High end"], ["Shadows", "Low", "Ultra"]]
    assert table.section == ["Game", "GPU tiers"]
    assert "steal()" not in d.model_dump_json() and "Noise" not in d.model_dump_json()
    assert d.images[0]["url"] == "https://example.org/benchmark.png"
    assert parse_html(d.url, html).content_hash == d.content_hash
    assert parse_html(d.url, html).revision_id != d.revision_id


def test_merged_cells_retain_column_alignment():
    d = parse_html("https://example.org", '<article><table><tr><th rowspan="2">Setting</th><th colspan="2">GPU</th></tr><tr><th>A</th><th>B</th></tr><tr><td>AA</td><td>FSR</td><td>DLSS</td></tr></table></article>')
    assert d.blocks[0].rows[:2] == [["Setting", "GPU", "GPU"], ["Setting", "A", "B"]]


def test_url_normalization_preserves_semantic_query():
    assert canonical_url("https://EXAMPLE.org/x?build=42&utm_source=mail#frag") == "https://example.org/x?build=42"
    with pytest.raises(ValueError):
        canonical_url("file:///secret")


def test_article_header_is_evidence_not_navigation_noise():
    doc = parse_html("https://example.org/", '<article><header><h1>Game guide</h1><p>Updated May 19</p></header><p>Settings</p></article>')
    assert any("Updated May 19" in b.text for b in doc.blocks)
