from pathlib import Path

from fastapi.testclient import TestClient

from funding.app import create_app
from funding.config import Settings


def test_health_and_proposals_api(tmp_path: Path):
    settings = Settings(
        secret_key="test-secret",
        database_path=tmp_path / "api.db",
        wallet_rpc_url="",
        bootstrap_admin_password="password123",
        bootstrap_admin_user="admin",
        bootstrap_admin_email="admin@example.com",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["wallet"] == "dry-run"

        r = client.get("/api/v1/proposals")
        assert r.status_code == 200
        assert r.json() == []

        r = client.get("/")
        assert r.status_code == 200
        assert b"Fund the work" in r.content
