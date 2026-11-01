import json
from unittest.mock import Mock

import pytest

from game_collector.providers import ExaSearch, ModelClient, ProviderError, PublicReader, parse_search_results
from tests.helpers import sample


def test_search_results_accept_text_and_mcp_wrapper():
    text = 'Title: Guide\nURL: https://example.org/guide\nText: Some text\nTitle: Patch\nURL: https://example.org/patch'
    assert len(parse_search_results(text)) == 2
    wrapper = {"content": [{"type": "text", "text": text}]}
    assert parse_search_results(wrapper)[0]["url"] == "https://example.org/guide"
    assert parse_search_results({"results": [{"url": "https://example.org/x", "title": "X"}]})[0]["title"] == "X"
    assert parse_search_results('nothing useful') == []


def test_exa_query_is_data_not_shell_code():
    runner = Mock(return_value=Mock(returncode=0, stdout='Title: G\nURL: https://example.org/g', stderr=''))
    adapter = ExaSearch(command=["node", "mcporter.js"], runner=runner)
    query = 'Game "; $(touch oops) `whoami`'
    assert adapter.search(query, 2)[0]["url"] == "https://example.org/g"
    call = runner.call_args
    assert call.kwargs["shell"] is False
    args = call.args[0]
    assert json.loads(args[args.index("--args") + 1])["query"] == query


def test_model_requires_configuration_and_keeps_error_sanitized(monkeypatch):
    monkeypatch.delenv("COLLECTOR_MODEL", raising=False)
    with pytest.raises(ProviderError, match="COLLECTOR_MODEL"):
        ModelClient()
    session = Mock()
    session.post.return_value = Mock(status_code=401, ok=False)
    model = ModelClient(model="test-model", api_key="TOP_SECRET", session=session)
    doc, _ = sample()
    with pytest.raises(ProviderError) as err:
        model.extract(doc, {"game_name": "Example Game"})
    assert "TOP_SECRET" not in str(err.value) and "401" in str(err.value)


def test_model_output_envelope_and_usage_are_retained():
    doc, extraction = sample()
    session = Mock()
    response = {"model": "actual-model", "usage": {"prompt_tokens": 100, "completion_tokens": 20}, "choices": [{"finish_reason": "stop", "message": {"content": extraction.model_dump_json()}}]}
    session.post.return_value = Mock(status_code=200, ok=True, json=lambda: response)
    client = ModelClient(model="requested-model", api_key="test", session=session)
    result = client.extract(doc, {"game_name": "Example Game"})
    assert result["data"]["game_match"] == "matched"
    assert result["model"] == "actual-model" and result["usage"]["prompt_tokens"] == 100
    payload = session.post.call_args.kwargs["json"]
    assert payload["response_format"] == {"type": "json_object"}
    assert session.post.call_args.kwargs["allow_redirects"] is False
    sent_doc = json.loads(payload["messages"][1]["content"])["document"]
    table = next(b for b in sent_doc["blocks"] if b["kind"] == "table")
    assert table["indexed_rows"][1]["cells"][1] == {"column_index": 1, "text": "High"}


def test_reader_rejects_non_public_urls_and_empty_pages():
    with pytest.raises(ProviderError):
        PublicReader().read("http://127.0.0.1/private")
    session = Mock()
    response = Mock(status_code=200, headers={"Content-Type": "text/html"}, url="https://example.org/", encoding="utf-8")
    response.content = b'<html><body><script>blocked</script></body></html>'
    response.text = response.content.decode()
    session.get.return_value = response
    with pytest.raises(ProviderError, match="readable"):
        PublicReader(session=session).read("https://example.org/")


def test_truncated_model_response_is_retained_for_review():
    doc, _ = sample()
    session = Mock()
    session.post.return_value = Mock(status_code=200, ok=True, json=lambda: {"model": "test", "usage": {"completion_tokens": 5}, "choices": [{"finish_reason": "length", "message": {"content": '{"game_match":'}}]})
    result = ModelClient(model="test", api_key="test", session=session).extract(doc, {})
    assert result["raw"] == '{"game_match":'
    assert result["incomplete_reason"] == "length"
