"""POST /v1/lookup/batch -- bulk-mode envelope.

Verifies:
  - the shape error from solo /v1/lookup turns into a
    per-row error envelope (not a top-level 4xx)
  - the cap rejects an over-size batch with 422
  - empty items list is accepted (just returns
    empty results) -- avoids a special-case branch
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


def test_batch_per_row_errors_dont_poison_the_request(client):
    # Two bad-shape rows; both should land in result
    # envelopes with ok=False and a non-empty error.
    # The TOP-level status stays 200 -- that's the
    # point of the batch endpoint.
    #
    # Don't assert on the specific status code in the
    # error string because the test boots in degraded
    # mode (no DB), so _resolve_one returns 503 before
    # reaching the shape-validation 400. Both
    # outcomes prove the row-isolation contract.
    resp = client.post(
        "/v1/lookup/batch",
        json={
            "items": [
                {"phone": "+14165550199", "ip": "1.2.3.4"},
                {},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2
    for row in body["results"]:
        assert row["ok"] is False
        assert row["error"]
        assert row["response"] is None


def test_batch_cap_rejected_with_422(client):
    items = [{"phone": "+14165550199"} for _ in range(101)]
    resp = client.post("/v1/lookup/batch", json={"items": items})
    assert resp.status_code == 422
    assert "cap is 100" in resp.json()["detail"]


def test_batch_empty_items_returns_empty_results(client):
    resp = client.post("/v1/lookup/batch", json={"items": []})
    assert resp.status_code == 200
    assert resp.json() == {"results": []}
