import pytest

from game_collector import config


@pytest.fixture(autouse=True)
def isolate_user_config(monkeypatch):
    """Synthetic tests must not depend on the developer's saved credentials."""
    monkeypatch.setattr(config, "_user_environment", lambda name: None)
