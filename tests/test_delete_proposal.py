"""Primary-admin delete proposal — blocked while escrow remains."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from funding.amounts import ATOMIC
from funding.app import create_app
from funding.assets import ASSET_PDC, parse_amount
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient
from tests.auth_helpers import TEST_TOTP_KEK, enable_totp, login_with_totp


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        secret_key="test-secret",
        database_path=tmp_path / "del.db",
        wallet_rpc_url="",
        bootstrap_admin_password="password123",
        bootstrap_admin_user="admin",
        bootstrap_admin_email="admin@example.com",
        session_https_only=False,
        funding_totp_kek=TEST_TOTP_KEK,
    )


def test_delete_empty_proposal(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        admin = svc.db.get_user_by_username("admin")
        assert admin is not None
        prop = svc.create_proposal(
            admin,
            headline="Spam",
            content="Remove me",
            category="misc",
            funds_target="1",
            payout_address="addr",
        )
        pid = prop.id
        svc.delete_proposal(admin, pid)
        assert svc.db.get_proposal(pid) is None
        assert svc.db.list_proposal_assets(pid) == []


def test_delete_blocked_with_available_escrow(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        admin = svc.db.get_user_by_username("admin")
        assert admin is not None
        prop = svc.create_proposal(
            admin,
            headline="Funded",
            content="Has escrow",
            category="misc",
            funds_target="1",
            payout_address="addr",
        )
        prop = svc.set_status(admin, prop.id, 2)
        assert prop.payment_id
        svc.db.credit_deposit(
            proposal_id=prop.id,
            payment_id=prop.payment_id,
            tx_hash="aa" * 32,
            amount_atomic=ATOMIC,
            block_height=1,
            asset=ASSET_PDC,
        )
        with pytest.raises(ValueError, match="escrow"):
            svc.delete_proposal(admin, prop.id)
        assert svc.db.get_proposal(prop.id) is not None


def test_http_delete_primary_admin_only(tmp_path: Path):
    settings = _settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        admin = svc.db.get_user_by_username("admin")
        assert admin is not None
        enable_totp(svc, admin.id)
        prop = svc.create_proposal(
            admin,
            headline="Http del",
            content="C",
            category="misc",
            funds_target="1",
            payout_address="addr",
        )
        user = svc.register("bob", "b@example.com", "password123")
        enable_totp(svc, user.id)
        login_with_totp(client, "bob", "password123")
        page = client.get(f"/proposals/{prop.id}")
        assert b"Delete proposal" not in page.content
        import re

        m = re.search(rb'name="csrf_token" value="([a-f0-9]+)"', page.content)
        assert m
        token = m.group(1).decode()
        denied = client.post(
            f"/proposals/{prop.id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert denied.status_code == 403
        assert svc.db.get_proposal(prop.id) is not None

        login_with_totp(client, "admin", "password123")
        page = client.get(f"/proposals/{prop.id}")
        assert b"Delete proposal" in page.content
        m = re.search(rb'name="csrf_token" value="([a-f0-9]+)"', page.content)
        assert m
        token = m.group(1).decode()
        ok = client.post(
            f"/proposals/{prop.id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert ok.headers.get("location") == "/"
        assert svc.db.get_proposal(prop.id) is None


def test_delete_after_full_payout(tmp_path: Path):
    """History may be removed once available escrow is zero."""
    settings = _settings(tmp_path)
    db = Database(settings.database_path)
    svc = FundingService(settings, db, WalletClient(settings))
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Done",
        content="C",
        category="misc",
        funds_target="0.02",
        payout_address="dest",
    )
    prop = svc.set_status(admin, prop.id, 2)
    fee = settings.transfer_fee_atomic
    amt = parse_amount("0.02", ASSET_PDC)
    svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=prop.payment_id,
        tx_hash="cc" * 32,
        amount_atomic=amt,
        block_height=1,
        asset=ASSET_PDC,
    )
    out = svc.payout_milestone(
        admin, prop.id, amount="0.02", asset=ASSET_PDC, milestone_note="done"
    )
    assert out["send_atomic"] == amt - fee
    assert svc.available_atomic(prop, ASSET_PDC) == 0
    svc.delete_proposal(admin, prop.id)
    assert svc.db.get_proposal(prop.id) is None
