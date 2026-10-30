"""Authorization: non-admin cannot pay out, change status, or steal admin via register."""

from pathlib import Path

from fastapi.testclient import TestClient

from funding.amounts import ATOMIC
from funding.app import create_app
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient
from tests.auth_helpers import TEST_TOTP_KEK, enable_totp, login_with_totp


def _settings(tmp_path: Path, **extra) -> Settings:
    kw = dict(
        secret_key="test-secret",
        database_path=tmp_path / "authz.db",
        wallet_rpc_url="",
        bootstrap_admin_password="password123",
        bootstrap_admin_user="admin",
        bootstrap_admin_email="admin@example.com",
        session_https_only=False,
        funding_totp_kek=TEST_TOTP_KEK,
    )
    kw.update(extra)
    return Settings(**kw)


def test_public_register_not_admin_when_bootstrap_configured(tmp_path: Path):
    settings = _settings(tmp_path)
    # Bootstrap already created admin via create_app lifespan.
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        assert svc.db.get_user_by_username("admin") is not None
        # Direct service register (same rules as HTTP).
        user = svc.register("eve", "eve@example.com", "password123")
        assert not user.is_admin
        try:
            svc.payout_milestone(user, 1, amount="0.1")
            assert False
        except PermissionError:
            pass
        try:
            svc.set_status(user, 1, 2)
            assert False
        except (PermissionError, KeyError):
            pass


def test_forged_is_admin_flag_cannot_payout(tmp_path: Path):
    """Even with is_admin=1 in DB, username must match BOOTSTRAP_ADMIN_USER."""
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        admin = svc.db.get_user_by_username("admin")
        assert admin is not None
        impostor = svc.register("hacker", "h@example.com", "password123")
        with svc.db._lock, svc.db._connect() as conn:
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (impostor.id,))
        impostor = svc.db.get_user(impostor.id)
        assert impostor is not None and impostor.is_admin
        prop = svc.create_proposal(
            admin,
            headline="X",
            content="Y",
            category="misc",
            funds_target="1",
            payout_address="addr",
        )
        prop = svc.set_status(admin, prop.id, 2)
        assert prop.payment_id
        svc.db.credit_deposit(
            proposal_id=prop.id,
            payment_id=prop.payment_id,
            tx_hash="aa",
            amount_atomic=ATOMIC,
            block_height=1,
        )
        try:
            svc.payout_milestone(impostor, prop.id, amount="0.5")
            assert False, "impostor payout must fail"
        except PermissionError:
            pass
        try:
            svc.set_status(impostor, prop.id, 3)
            assert False
        except PermissionError:
            pass
        assert client.get("/").status_code == 200


def test_http_payout_forbidden_for_non_admin(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        admin = svc.db.get_user_by_username("admin")
        assert admin is not None
        enable_totp(svc, admin.id)
        prop = svc.create_proposal(
            admin,
            headline="P",
            content="C",
            category="misc",
            funds_target="1",
            payout_address="addr",
        )
        prop = svc.set_status(admin, prop.id, 2)
        assert prop.payment_id
        svc.db.credit_deposit(
            proposal_id=prop.id,
            payment_id=prop.payment_id,
            tx_hash="bb",
            amount_atomic=ATOMIC,
            block_height=1,
        )
        # Register + login as normal user (bootstrap password set → not admin).
        user = svc.register("bob", "b@example.com", "password123")
        assert not user.is_admin
        enable_totp(svc, user.id)
        login_with_totp(client, "bob", "password123")
        # Visit proposal to mint CSRF into session.
        page = client.get(f"/proposals/{prop.id}")
        assert page.status_code == 200
        assert b"Pay out" not in page.content
        # Steal csrf from session by scraping any form — comment form has it.
        assert b'name="csrf_token"' in page.content
        import re

        m = re.search(rb'name="csrf_token" value="([a-f0-9]+)"', page.content)
        assert m
        token = m.group(1).decode()
        denied = client.post(
            f"/proposals/{prop.id}/payout",
            data={
                "amount": "0.5",
                "to_address": "addr",
                "milestone_note": "x",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert denied.status_code == 403
        status_denied = client.post(
            f"/proposals/{prop.id}/status",
            data={"new_status": "3", "csrf_token": token},
            follow_redirects=False,
        )
        assert status_denied.status_code == 403
        # Escrow untouched.
        refreshed = svc.db.get_proposal(prop.id)
        assert refreshed is not None
        assert svc.progress(refreshed)["paid_atomic"] == 0


def test_payout_rejects_missing_csrf(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        admin = svc.db.get_user_by_username("admin")
        assert admin is not None
        enable_totp(svc, admin.id)
        login_with_totp(client, "admin", "password123")
        prop = svc.create_proposal(
            admin,
            headline="P",
            content="C",
            category="misc",
            funds_target="1",
            payout_address="addr",
        )
        prop = svc.set_status(admin, prop.id, 2)
        bad = client.post(
            f"/proposals/{prop.id}/payout",
            data={"amount": "0.5", "to_address": "addr", "csrf_token": "wrong"},
            follow_redirects=False,
        )
        assert bad.status_code == 403
