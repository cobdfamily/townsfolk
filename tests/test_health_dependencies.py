"""GET /v1/health/dependencies -- deep DB probe.

Pre-v0.4 /v1/health just did SELECT 1 (connection
works). v0.4 adds row counts for the three tables
(exchanges, places, ip_ranges) so a dashboard can
spot a stale ETL.

This unit test boots in degraded mode (no real DB)
so the response shape we pin is the no-pool case:
ok=false + error + config summary. Live-DB testing
happens in the integration suite.
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


def test_returns_200_with_ok_false_when_db_is_down(client):
    # v0.4 contract: this endpoint NEVER returns 503.
    # The probe ran; the result is just "the DB is
    # not ready right now." Caller branches on ok in
    # the body.
    resp = client.get("/v1/health/dependencies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "error" in body
    # Config summary surfaces so callers can see what
    # radius_km defaults are in effect.
    assert body["config"]["radius_default_km"] == 100
    assert body["config"]["radius_ceiling_km"] == 500
