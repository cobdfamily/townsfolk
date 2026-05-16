"""X-Request-ID middleware -- caller-supplied id is
echoed back; missing id gets a fresh one minted; very
long ids are rejected (replaced by a minted one) so
malicious headers can't blow up log lines.
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


def test_request_id_minted_when_absent(client):
    r = client.get("/")
    assert r.status_code == 200
    rid = r.headers.get("X-Request-ID")
    # 32-char hex = uuid4().hex
    assert rid and len(rid) == 32 and all(
        c in "0123456789abcdef" for c in rid
    )


def test_request_id_echoed_when_provided(client):
    r = client.get(
        "/",
        headers={"X-Request-ID": "trace-abc-123"},
    )
    assert r.headers["X-Request-ID"] == "trace-abc-123"


def test_request_id_overflow_is_replaced(client):
    # 129 chars -- past our 128 cap.
    huge = "x" * 129
    r = client.get("/", headers={"X-Request-ID": huge})
    assert r.headers["X-Request-ID"] != huge
    assert len(r.headers["X-Request-ID"]) == 32
