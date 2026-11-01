"""Shared fixtures for offline product tests."""

from pathlib import Path

import pytest

from game_app.repository import ProductRepository
from tests.product.support.fixtures import FixtureLoader

PRODUCT_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "product"


@pytest.fixture
def product_db(tmp_path) -> ProductRepository:
    repository = ProductRepository(tmp_path / "product.sqlite3")
    repository.migrate()
    yield repository
    repository.close()


@pytest.fixture
def fixture_loader() -> FixtureLoader:
    return FixtureLoader(PRODUCT_FIXTURES)
