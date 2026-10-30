from pathlib import Path

from fastapi.testclient import TestClient

from funding.app import create_app
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient
from tests.auth_helpers import TEST_TOTP_KEK, enable_totp, login_with_totp


def test_change_password(tmp_path: Path):
    db_path = tmp_path / "pw.db"
    settings = Settings(
        secret_key="test-secret",
        database_path=db_path,
        wallet_rpc_url="",
        bootstrap_admin_password="password123",
        bootstrap_admin_user="admin",
        bootstrap_admin_email="admin@example.com",
        session_https_only=False,
        funding_totp_kek=TEST_TOTP_KEK,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        admin = app.state.db.get_user_by_username("admin")
        assert admin is not None
        enable_totp(app.state.service, admin.id)
        login_with_totp(client, "admin", "password123")

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
        assert bad.headers.get("location") == "/account/password"

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
        assert ok.headers.get("location") == "/"

        client.post("/logout", follow_redirects=False)
        old_login = client.post(
            "/login",
            data={"username": "admin", "password": "password123"},
            follow_redirects=False,
        )
        assert old_login.headers.get("location") == "/login"
        login_with_totp(client, "admin", "newpass123")

    db = Database(db_path)
    svc = FundingService(settings, db, WalletClient(settings))
    user = db.get_user_by_username("admin")
    assert user is not None
    assert svc.verify_password("newpass123", user.password_hash)
    assert not svc.verify_password("password123", user.password_hash)
