from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings


@pytest.fixture
def app_factory(tmp_path: Path):
    def factory(**overrides):
        settings = Settings(
            database_path=overrides.pop("database_path", tmp_path / "naftamon-test.sqlite3"),
            max_request_bytes=overrides.pop("max_request_bytes", 8 * 1024 * 1024),
            offline_after_seconds=overrides.pop("offline_after_seconds", 900),
            source_inactive_after_seconds=overrides.pop("source_inactive_after_seconds", 86400),
            history_retention_days=overrides.pop("history_retention_days", 90),
        )
        return create_app(settings=settings)
    return factory


@pytest.fixture
def client(app_factory):
    app = app_factory()
    with TestClient(app) as test_client:
        yield test_client
