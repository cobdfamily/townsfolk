"""GET /v1/data-version -- size + freshness signal.

Boots in degraded mode; pins the shape the endpoint
returns when the pool isn't up. Live-DB tests of the
xmin tracking happen in the integration suite.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://nope:nope@127.0.0.1:1/nope",
    )
    import importlib
    from townsfolk import main as main_module
    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


def test_returns_ok_false_with_error_when_db_down(client):
    resp = client.get("/v1/data-version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "error" in body
