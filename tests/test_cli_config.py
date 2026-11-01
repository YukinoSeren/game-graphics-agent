import json
from unittest.mock import Mock

from game_collector import config
from game_collector.__main__ import main
from game_collector.providers import ModelClient
from tests.helpers import sample


def test_user_environment_fallback_and_process_override(monkeypatch):
    monkeypatch.setattr(config, "_user_environment", lambda name: "saved-value")
    monkeypatch.delenv("COLLECTOR_TEST", raising=False)
    assert config.getenv("COLLECTOR_TEST") == "saved-value"
    monkeypatch.setenv("COLLECTOR_TEST", "current-value")
    assert config.getenv("COLLECTOR_TEST") == "current-value"


def test_deepseek_uses_selected_model_and_json_output(monkeypatch):
    monkeypatch.setenv("COLLECTOR_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("COLLECTOR_API_BASE", "https://api.deepseek.com")
    monkeypatch.setenv("COLLECTOR_API_KEY_ENV", "DEEPSEEK_API_KEY")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "SECRET")
    session = Mock()
    doc, extraction = sample()
    session.post.return_value = Mock(ok=True, json=lambda: {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {"content": extraction.model_dump_json()}}]})
    client = ModelClient(session=session)
    result = client.extract(doc, {"game_name": "Example Game"})
    call = session.post.call_args
    assert call.args[0] == "https://api.deepseek.com/chat/completions"
    assert call.kwargs["json"]["model"] == "deepseek-v4-flash"
    assert call.kwargs["json"]["max_tokens"] >= 16000
    assert result["model"] == "deepseek-flash"


def test_cli_needs_game_and_seeded_urls(capsys):
    assert main(["--mode", "seeded", "--game", "Example Game", "--read-only"]) == 1
    assert "--url" in capsys.readouterr().err


def test_cli_help_is_available_without_credentials(capsys):
    import pytest
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "--check-model" in capsys.readouterr().out
