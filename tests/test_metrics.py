"""/metrics endpoint smoke. Doesn't need a real DB
-- the app boots in degraded mode (db_ready=False)
and /metrics still answers from the in-process
counter bag.

Pins:
  - the exposition format is text/plain (not JSON)
  - the four counter names brian/dashboards rely on
    show up (lookups_total, by_mode_total,
    lookup_errors_total, db_ready gauge)
  - per-mode labels include all four modes (phone,
    ip, coords, fallback)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    # Point at an unreachable DB so the lifespan
    # ends in db_ready=False -- /metrics doesn't
    # need the pool. Avoids requiring a live
    # Postgres for the unit test.
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://nope:nope@127.0.0.1:1/nope",
    )
    # Explicitly blank the cache URL so the test
    # doesn't accidentally pick up a real Redis
    # from the host env. cache_ready should read 0.
    monkeypatch.setenv("TOWNSFOLK_REDIS_URL", "")
    import importlib
    from townsfolk import main as main_module
    importlib.reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


def test_metrics_exposes_required_counters(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "townsfolk_lookups_total 0" in body
    assert "townsfolk_lookup_errors_total 0" in body
    assert "townsfolk_lookup_cache_hits_total 0" in body
    assert "townsfolk_lookup_cache_misses_total 0" in body
    assert "townsfolk_db_ready 0" in body  # boot was degraded
    assert "townsfolk_cache_ready 0" in body  # boot was degraded
    for mode in ("phone", "ip", "coords", "fallback"):
        assert f'mode="{mode}"' in body


def test_metrics_is_plain_text(client):
    resp = client.get("/metrics")
    assert resp.headers["content-type"].startswith("text/plain")
