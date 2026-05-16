"""GET /v1/exchanges/{npa}/{nxx} -- direct exchange
lookup.

Boots in degraded mode; tests pin the shape errors
(400 / 503) since the lookup itself requires a live
DB. Live-DB success-path tests live in integration.
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


def test_400_when_npa_is_not_three_digits(client):
    r = client.get("/v1/exchanges/12/200")
    assert r.status_code in (400, 422)


def test_400_when_nxx_is_not_three_digits(client):
    r = client.get("/v1/exchanges/204/2")
    assert r.status_code in (400, 422)


def test_400_when_either_contains_letters(client):
    r = client.get("/v1/exchanges/abc/200")
    assert r.status_code == 400


def test_503_when_db_down(client):
    r = client.get("/v1/exchanges/204/200")
    assert r.status_code == 503
