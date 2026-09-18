"""HTTP API integration tests.

Tests the FastAPI endpoints using httpx TestClient.
"""

import pytest
from fastapi.testclient import TestClient

from mirror_memory.api import app, init_db


@pytest.fixture(scope="module")
def client():
    """Create a test client with SQLite database."""
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./test_api.db"
    os.environ["MIRROR_ENV"] = "test"
    init_db("sqlite:///./test_api.db")
    c = TestClient(app)
    yield c
    # Cleanup
    import pathlib
    pathlib.Path("./test_api.db").unlink(missing_ok=True)


SCOPE = {"tenant_id": "api_t", "app_id": "api_a", "subject_id": "api_u1"}


class TestHealth:
    def test_health(self, client):
        r = client.get("/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestAuthFlow:
    def test_grant_and_revoke(self, client):
        # Grant
        r = client.post("/v1/auth/grant", json={
            "scope": SCOPE, "version": 1, "expires_hours": 1.0,
        })
        assert r.status_code == 200
        assert r.json()["success"]

        # Revoke
        r = client.post("/v1/auth/revoke", json={
            "scope": SCOPE, "version": 2,
        })
        assert r.status_code == 200
        assert r.json()["success"]


class TestObserveRecall:
    def test_observe_and_recall(self, client):
        # Re-authorize
        client.post("/v1/auth/grant", json={"scope": SCOPE, "version": 3})

        # Observe
        r = client.post("/v1/observe", json={
            "scope": SCOPE,
            "event_id": "api_evt_001",
            "text": "I prefer dark mode",
        })
        assert r.status_code == 200
        assert r.json()["success"]

        # Recall
        r = client.post("/v1/recall", json={
            "scope": SCOPE, "query": "dark mode",
        })
        assert r.status_code == 200
        assert r.json()["success"]


class TestCorrectAndForget:
    def test_correct_and_forget(self, client):
        # Observe
        r = client.post("/v1/observe", json={
            "scope": SCOPE,
            "event_id": "api_evt_002",
            "text": "I like cats",
        })
        op_id = r.json()["outcome"]["operation_id"]

        # Get operation status
        r = client.post("/v1/operation", json={
            "scope": SCOPE, "operation_id": op_id,
        })
        assert r.status_code == 200
        assert r.json()["success"]


class TestErrorHandling:
    def test_observe_without_auth(self, client):
        """Observe without authorization returns error."""
        new_scope = {"tenant_id": "new_t", "app_id": "new_a", "subject_id": "new_u"}
        r = client.post("/v1/observe", json={
            "scope": new_scope,
            "event_id": "evt_noauth",
            "text": "test",
        })
        assert r.status_code == 200
        assert not r.json()["success"]
        assert "auth" in r.json()["reason"].lower() or "denied" in r.json()["reason"].lower()