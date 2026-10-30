from pathlib import Path

from fastapi.testclient import TestClient

from funding.app import create_app
from funding.config import Settings
from funding.totp import current_code, decrypt_secret, derive_kek, encrypt_secret, verify_code
from tests.auth_helpers import TEST_TOTP_KEK, TEST_TOTP_SECRET, enable_totp, login_with_totp


def _settings(tmp_path: Path, **extra) -> Settings:
    kw = dict(
        secret_key="test-secret",
        database_path=tmp_path / "totp.db",
        wallet_rpc_url="",
        bootstrap_admin_password="password123",
        bootstrap_admin_user="admin",
        bootstrap_admin_email="admin@example.com",
        session_https_only=False,
        funding_totp_kek=TEST_TOTP_KEK,
    )
    kw.update(extra)
    return Settings(**kw)


def test_encrypt_roundtrip():
    kek = derive_kek(TEST_TOTP_KEK)
    blob = encrypt_secret(TEST_TOTP_SECRET, kek)
    assert decrypt_secret(blob, kek) == TEST_TOTP_SECRET
    assert verify_code(TEST_TOTP_SECRET, current_code(TEST_TOTP_SECRET))


def test_register_requires_totp_enroll(tmp_path: Path):
    app = create_app(_settings(tmp_path, bootstrap_admin_password=""))
    with TestClient(app) as client:
        r = client.post(
            "/register",
            data={
                "username": "alice",
                "email": "a@example.com",
                "password": "password123",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers.get("location") == "/account/2fa/enroll"
        page = client.get("/account/2fa/enroll")
        assert page.status_code == 200
        assert b"Set up authenticator" in page.content
        assert b"data:image/png;base64," in page.content
        # Session must not be fully logged in yet.
        home = client.get("/")
        assert b"Log in" in home.content


def test_login_totp_and_admin_users(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        admin = svc.db.get_user_by_username("admin")
        assert admin is not None
        enable_totp(svc, admin.id)
        login_with_totp(client, "admin", "password123")

        users_page = client.get("/admin/users")
        assert users_page.status_code == 200
        assert b"admin@example.com" in users_page.content
        assert b"Registered users" in users_page.content
        import re

        m = re.search(rb'name="csrf_token" value="([a-f0-9]+)"', users_page.content)
        assert m
        csrf = m.group(1).decode()
        reset = client.post(
            f"/admin/users/{admin.id}/reset-password",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert reset.status_code == 303
        # Follow to capture flash with new password is awkward; check hash changed.
        admin2 = svc.db.get_user(admin.id)
        assert admin2 is not None
        assert not svc.verify_password("password123", admin2.password_hash)

        client.post("/logout", follow_redirects=False)
        # After reset, old password fails.
        bad = client.post(
            "/login",
            data={"username": "admin", "password": "password123"},
            follow_redirects=False,
        )
        assert bad.headers.get("location") == "/login"


def test_users_panel_forbidden_for_non_admin(tmp_path: Path):
    settings = _settings(tmp_path, bootstrap_admin_password="")
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        user = svc.register("alice", "a@example.com", "password123")
        # First registrant becomes is_admin; still not the bootstrap "admin" username.
        assert user.is_admin
        assert user.username.lower() != settings.bootstrap_admin_user.lower()
        enable_totp(svc, user.id)
        login_with_totp(client, "alice", "password123")
        denied = client.get("/admin/users")
        assert denied.status_code == 403
        assert b"Users</a>" not in client.get("/").content
