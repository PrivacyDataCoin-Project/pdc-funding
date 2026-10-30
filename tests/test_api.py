import sqlite3
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

        assert client.post("/api/v1/scan").status_code == 404
        assert client.post("/api/v1/proposals").status_code == 405
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404

        r = client.get("/")
        assert r.status_code == 200
        assert b"Fund the work" in r.content
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_missing_session_user_does_not_500(tmp_path: Path):
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
        login = client.post(
            "/login",
            data={"username": "admin", "password": "password123"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        conn = sqlite3.connect(settings.database_path)
        conn.execute("DELETE FROM users")
        conn.commit()
        conn.close()
        page = client.get("/")
        assert page.status_code == 200
        assert b"Log in" in page.content
