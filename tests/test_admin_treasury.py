"""Admin treasury panel: hot wallets + per-proposal escrow."""

from pathlib import Path

from fastapi.testclient import TestClient

from funding.app import create_app
from funding.assets import ASSET_BTC, ASSET_BTCX, ASSET_PDC, parse_amount
from funding.btc_wallet import BtcWalletClient
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient
from tests.auth_helpers import TEST_TOTP_KEK, enable_totp, login_with_totp


def _svc(tmp_path: Path) -> FundingService:
    settings = Settings(
        secret_key="treasury-test",
        database_path=tmp_path / "t.db",
        wallet_rpc_url="",
        btc_rpc_url="",
        bootstrap_admin_password="",
        bootstrap_admin_user="admin",
        funding_totp_kek=TEST_TOTP_KEK,
        transfer_fee_atomic=10_000_000_000,
    )
    db = Database(settings.database_path)
    return FundingService(
        settings, db, WalletClient(settings), BtcWalletClient(settings)
    )


def test_treasury_stats_hot_and_per_proposal(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Fund me",
        content="Work",
        category="misc",
        assets=[ASSET_PDC, ASSET_BTCX, ASSET_BTC],
        targets={
            ASSET_PDC: "10",
            ASSET_BTCX: "0.01",
            ASSET_BTC: "0.001",
        },
        payout_addresses={
            ASSET_PDC: "pdcDest",
            ASSET_BTCX: "pdcDest",
            ASSET_BTC: "bc1qdest",
        },
    )
    prop = svc.set_status(admin, prop.id, 2)
    pdc = svc.db.get_proposal_asset(prop.id, ASSET_PDC)
    btcx = svc.db.get_proposal_asset(prop.id, ASSET_BTCX)
    btc = svc.db.get_proposal_asset(prop.id, ASSET_BTC)
    svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=pdc.payment_id,
        tx_hash="aa" * 32,
        amount_atomic=parse_amount("2", ASSET_PDC),
        block_height=1,
        asset=ASSET_PDC,
    )
    svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=btcx.payment_id,
        tx_hash="bb" * 32,
        amount_atomic=parse_amount("0.01", ASSET_BTCX),
        block_height=1,
        asset=ASSET_BTCX,
    )
    svc.db.credit_btc_cumulative(
        proposal_id=prop.id,
        btc_address=btc.btc_address,
        chain_received_sats=parse_amount("0.001", ASSET_BTC),
        block_height=1,
    )

    stats = svc.treasury_stats()
    assert stats["hot"][ASSET_PDC]["mode"] == "dry-run"
    assert stats["hot"][ASSET_BTC]["mode"] == "dry-run"
    assert stats["escrow_totals"][ASSET_PDC]["available"] == parse_amount("2", ASSET_PDC)
    assert stats["escrow_totals"][ASSET_BTCX]["available"] == parse_amount(
        "0.01", ASSET_BTCX
    )
    assert stats["escrow_totals"][ASSET_BTC]["available"] == parse_amount(
        "0.001", ASSET_BTC
    )
    assert len(stats["proposals"]) == 1
    assert len(stats["proposals"][0]["assets"]) == 3


def test_treasury_page_primary_admin_only(tmp_path: Path):
    settings = Settings(
        secret_key="treasury-http",
        database_path=tmp_path / "http.db",
        wallet_rpc_url="",
        btc_rpc_url="",
        bootstrap_admin_password="password123",
        bootstrap_admin_user="admin",
        bootstrap_admin_email="admin@example.com",
        funding_totp_kek=TEST_TOTP_KEK,
        session_https_only=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        admin = svc.db.get_user_by_username("admin")
        assert admin is not None
        enable_totp(svc, admin.id)
        other = svc.register("bob", "b@example.com", "password123")
        enable_totp(svc, other.id)

        login_with_totp(client, "bob", "password123")
        r = client.get("/admin/treasury")
        assert r.status_code == 403

        client.post("/logout")
        login_with_totp(client, "admin", "password123")
        r = client.get("/admin/treasury")
        assert r.status_code == 200
        assert b"Treasury" in r.content
        assert b"Main wallets" in r.content
        assert b"Per funding proposal" in r.content
