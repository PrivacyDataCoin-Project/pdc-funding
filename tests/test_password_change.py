from pathlib import Path

from fastapi.testclient import TestClient

from funding.app import create_app
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient


def test_change_password(tmp_path: Path):
    settings = Settings(
        secret_key="test-secret",
        database_path=tmp_path / "pw.db",
        wallet_rpc_url="",
        bootstrap_admin_password="password123",
        bootstrap_admin_user="admin",
        bootstrap_admin_email="admin@example.com",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post(
            "/login",
            data={"username": "admin", "password": "password123"},
            follow_redirects=False,
        ).status_code == 303
        bad = client.post(
            "/account/password",
            data={
                "current_password": "wrong",
                "new_password": "newpass123",
                "new_password2": "newpass123",
            },
            follow_redirects=False,
        )
        assert bad.status_code == 303
        ok = client.post(
            "/account/password",
            data={
                "current_password": "password123",
                "new_password": "newpass123",
                "new_password2": "newpass123",
            },
            follow_redirects=False,
        )
        assert ok.status_code == 303
        client.post("/logout", follow_redirects=False)
        assert client.post(
            "/login",
            data={"username": "admin", "password": "newpass123"},
            follow_redirects=False,
        ).status_code == 303

    db = Database(settings.database_path)
    svc = FundingService(settings, db, WalletClient(settings))
    user = db.get_user_by_username("admin")
    assert user is not None
    assert svc.verify_password("newpass123", user.password_hash)
