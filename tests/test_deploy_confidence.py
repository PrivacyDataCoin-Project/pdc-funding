"""Adversarial / deploy-confidence suite — no live chain spend."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from funding.assets import (
    ASSET_BTC,
    ASSET_BTCX,
    ASSET_PDC,
    parse_amount,
    resolve_payment_ticker,
)
from funding.btc_wallet import BtcWalletClient
from funding.config import Settings
from funding.db import Database
from funding.scanner import DepositScanner
from funding.service import FundingService
from funding.wallet import Payment, WalletClient, WalletRejected


def _settings(tmp_path: Path, **kwargs) -> Settings:
    base = dict(
        secret_key="deploy-confidence-secret",
        database_path=tmp_path / "conf.db",
        wallet_rpc_url="",
        btc_rpc_url="",
        bootstrap_admin_password="",
        bootstrap_admin_user="admin",
        transfer_fee_atomic=10_000_000_000,
        btcx_transfer_fee_atomic=0,
        btc_transfer_fee_sats=1000,
    )
    base.update(kwargs)
    return Settings(**base)


def _svc(tmp_path: Path, **kwargs) -> FundingService:
    settings = _settings(tmp_path, **kwargs)
    db = Database(settings.database_path)
    return FundingService(
        settings, db, WalletClient(settings), BtcWalletClient(settings)
    )


def test_legacy_sqlite_migrates_to_multi_asset(tmp_path: Path):
    """Simulate pre-multi-asset DB then open with new Database migrator."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            headline TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL,
            status INTEGER NOT NULL DEFAULT 1,
            funds_target_atomic INTEGER NOT NULL DEFAULT 0,
            payout_address TEXT NOT NULL DEFAULT '',
            payment_id TEXT UNIQUE,
            integrated_address TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE deposits (
            id INTEGER PRIMARY KEY,
            proposal_id INTEGER NOT NULL,
            payment_id TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            amount_atomic INTEGER NOT NULL,
            block_height INTEGER NOT NULL DEFAULT 0,
            credited_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(tx_hash, payment_id)
        );
        CREATE TABLE payouts (
            id INTEGER PRIMARY KEY,
            proposal_id INTEGER NOT NULL,
            amount_atomic INTEGER NOT NULL,
            to_address TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            milestone_note TEXT NOT NULL DEFAULT '',
            created_by INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE comments (
            id INTEGER PRIMARY KEY,
            proposal_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO users (id, username, email, password_hash, is_admin)
        VALUES (1, 'admin', 'a@example.com', 'x', 1);
        INSERT INTO proposals (
            id, user_id, headline, content, category, status,
            funds_target_atomic, payout_address, payment_id, integrated_address
        ) VALUES (
            1, 1, 'Legacy', 'Body', 'misc', 2,
            1000000000000, 'pdcDest', 'aabbccdd', 'PxINTEGRATED'
        );
        INSERT INTO deposits (
            proposal_id, payment_id, tx_hash, amount_atomic, block_height
        ) VALUES (1, 'aabbccdd', 'deadbeef', 500000000000, 10);
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    rows = db.list_proposal_assets(1)
    assert len(rows) == 1
    assert rows[0].asset == ASSET_PDC
    assert rows[0].payment_id == "aabbccdd"
    assert rows[0].received_atomic == 500_000_000_000
    deps = db.list_deposits(1)
    assert len(deps) == 1
    assert deps[0].asset == ASSET_PDC


def test_credit_deposit_rejects_foreign_payment_id(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Own",
        content="x",
        category="misc",
        assets=[ASSET_PDC],
        targets={ASSET_PDC: "1"},
        payout_addresses={ASSET_PDC: "dest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    assert (
        svc.db.credit_deposit(
            proposal_id=prop.id,
            payment_id="not-this-proposals-pid",
            tx_hash="aa" * 32,
            amount_atomic=parse_amount("1", ASSET_PDC),
            block_height=1,
            asset=ASSET_PDC,
        )
        is False
    )


def test_duplicate_btc_address_rejected(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    a = svc.create_proposal(
        admin,
        headline="A",
        content="x",
        category="misc",
        assets=[ASSET_BTC],
        targets={ASSET_BTC: "0.01"},
        payout_addresses={ASSET_BTC: "bc1qa"},
    )
    b = svc.create_proposal(
        admin,
        headline="B",
        content="x",
        category="misc",
        assets=[ASSET_BTC],
        targets={ASSET_BTC: "0.01"},
        payout_addresses={ASSET_BTC: "bc1qb"},
    )
    assert svc.db.set_asset_btc_address_if_empty(a.id, "bc1qshared")
    with pytest.raises(RuntimeError, match="btc_address already"):
        svc.db.set_asset_btc_address_if_empty(b.id, "bc1qshared")


def test_bulk_payments_keeps_both_assets_same_tx():
    settings = Settings(secret_key="x", wallet_rpc_url="")
    wallet = WalletClient(settings)
    # Force parse path via monkeypatch of enabled + _call
    raw = {
        "payments": [
            {
                "payment_id": "pid1",
                "tx_hash": "aa" * 32,
                "amount": 10,
                "block_height": 1,
                "unlock_time": 0,
                "asset_id": settings.pdc_asset_id,
            },
            {
                "payment_id": "pid1",
                "tx_hash": "aa" * 32,
                "amount": 20,
                "block_height": 1,
                "unlock_time": 0,
                "asset_id": settings.btcx_asset_id,
            },
        ]
    }

    class W(WalletClient):
        @property
        def enabled(self) -> bool:
            return True

        def _call(self, method, params=None):
            assert method == "get_bulk_payments"
            return raw

    out = W(settings).get_bulk_payments(["pid1"])
    assert len(out) == 2
    assets = {p.asset_id for p in out}
    assert settings.pdc_asset_id.lower() in assets
    assert settings.btcx_asset_id.lower() in assets


def test_resolve_payment_ticker_matrix():
    settings = Settings(secret_key="x")
    assert (
        resolve_payment_ticker(settings, "", proposal_tickers={ASSET_PDC}) == ASSET_PDC
    )
    assert (
        resolve_payment_ticker(
            settings, "", proposal_tickers={ASSET_PDC, ASSET_BTCX}
        )
        is None
    )
    assert (
        resolve_payment_ticker(
            settings, "", proposal_tickers={ASSET_BTCX}
        )
        is None
    )
    assert (
        resolve_payment_ticker(
            settings,
            settings.btcx_asset_id,
            proposal_tickers={ASSET_PDC, ASSET_BTCX},
        )
        == ASSET_BTCX
    )


def test_btc_payout_never_calls_l1_transfer(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="BTC only",
        content="x",
        category="misc",
        assets=[ASSET_BTC],
        targets={ASSET_BTC: "0.01"},
        payout_addresses={ASSET_BTC: "bc1qdest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    addr = svc.db.get_proposal_asset(prop.id, ASSET_BTC).btc_address
    assert (
        svc.db.credit_btc_cumulative(
            proposal_id=prop.id,
            btc_address=addr,
            chain_received_sats=parse_amount("0.01", ASSET_BTC),
            block_height=1,
        )
        > 0
    )

    class BoomL1(WalletClient):
        def transfer(self, *a, **k):
            raise AssertionError("L1 transfer must not run for BTC payout")

    class CapBtc(BtcWalletClient):
        def __init__(self, settings):
            super().__init__(settings)
            self.calls = []

        def send_to_address(self, address, amount_sats, *, comment="", **kwargs):
            self.calls.append((address, amount_sats, comment, kwargs))
            return "btc_dryrun_ok"

    svc.wallet = BoomL1(svc.settings)
    cap = CapBtc(svc.settings)
    svc.btc_wallet = cap
    out = svc.payout_milestone(
        admin, prop.id, amount="0.01", asset=ASSET_BTC, milestone_note="m"
    )
    assert out["tx_hash"] == "btc_dryrun_ok"
    assert len(cap.calls) == 1
    # Hard model: full escrow amount sent; miner fee subtracted from amount on-chain.
    assert cap.calls[0][1] == parse_amount("0.01", ASSET_BTC)


def test_concurrent_btc_cumulative_no_double(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Race",
        content="x",
        category="misc",
        assets=[ASSET_BTC],
        targets={ASSET_BTC: "1"},
        payout_addresses={ASSET_BTC: "bc1q"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    addr = svc.db.get_proposal_asset(prop.id, ASSET_BTC).btc_address
    results: list[int] = []

    def worker():
        results.append(
            svc.db.credit_btc_cumulative(
                proposal_id=prop.id,
                btc_address=addr,
                chain_received_sats=50_000,
                block_height=100,
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sum(results) == 50_000
    assert results.count(50_000) == 1
    assert svc.db.get_proposal_asset(prop.id, ASSET_BTC).received_atomic == 50_000


def test_concurrent_btc_payout_single_winner(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Pay race",
        content="x",
        category="misc",
        assets=[ASSET_BTC],
        targets={ASSET_BTC: "0.02"},
        payout_addresses={ASSET_BTC: "bc1qdest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    addr = svc.db.get_proposal_asset(prop.id, ASSET_BTC).btc_address
    svc.db.credit_btc_cumulative(
        proposal_id=prop.id,
        btc_address=addr,
        chain_received_sats=parse_amount("0.02", ASSET_BTC),
        block_height=1,
    )

    class SlowBtc(BtcWalletClient):
        def __init__(self, settings):
            super().__init__(settings)
            self.gate = threading.Event()
            self.started = threading.Event()
            self.n = 0

        def send_to_address(self, address, amount_sats, *, comment="", **kwargs):
            self.n += 1
            self.started.set()
            assert self.gate.wait(5)
            return f"btc_dryrun_{self.n}"

    slow = SlowBtc(svc.settings)
    svc.btc_wallet = slow
    ok: list = []
    err: list = []

    def pay():
        try:
            ok.append(
                svc.payout_milestone(
                    admin, prop.id, amount="0.02", asset=ASSET_BTC, milestone_note="x"
                )
            )
        except Exception as exc:  # noqa: BLE001
            err.append(exc)

    t1 = threading.Thread(target=pay)
    t2 = threading.Thread(target=pay)
    t1.start()
    assert slow.started.wait(5)
    t2.start()
    t2.join(5)
    assert not t2.is_alive()
    slow.gate.set()
    t1.join(5)
    assert len(ok) == 1
    assert len(err) == 1
    assert "available" in str(err[0]).lower() or "exceeds" in str(err[0]).lower()
    assert slow.n == 1


def test_scanner_btc_end_to_end_mock(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Scan BTC",
        content="x",
        category="misc",
        assets=[ASSET_BTC],
        targets={ASSET_BTC: "0.001"},
        payout_addresses={ASSET_BTC: "bc1q"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    addr = svc.db.get_proposal_asset(prop.id, ASSET_BTC).btc_address

    class MockBtc(BtcWalletClient):
        @property
        def enabled(self) -> bool:
            return True

        def get_blockchain_info(self):
            return {"blocks": 900_000}

        def get_received_by_address(self, address, minconf=1):
            assert address == addr
            return 12345

    live = _settings(tmp_path, btc_rpc_url="http://127.0.0.1:1", wallet_rpc_url="")
    scanner = DepositScanner(live, svc.db, WalletClient(live), MockBtc(live))
    assert scanner.scan_once() == 1
    assert scanner.scan_once() == 0  # idempotent
    assert svc.db.get_proposal_asset(prop.id, ASSET_BTC).received_atomic == 12345


def test_completed_status_still_scans(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Done",
        content="x",
        category="misc",
        assets=[ASSET_BTC],
        targets={ASSET_BTC: "0.001"},
        payout_addresses={ASSET_BTC: "bc1q"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    prop = svc.set_status(admin, prop.id, 4)
    assert prop.status == 4
    rows = svc.db.assets_needing_btc_scan()
    assert any(r.proposal_id == prop.id for r in rows)


def test_btcx_payout_checks_pdc_fee_balance(tmp_path: Path):
    svc = _svc(tmp_path)  # dry-run assign addresses first
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="BTCX fee",
        content="x",
        category="misc",
        assets=[ASSET_BTCX],
        targets={ASSET_BTCX: "0.05"},
        payout_addresses={ASSET_BTCX: "pdcDest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    row = svc.db.get_proposal_asset(prop.id, ASSET_BTCX)
    svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=row.payment_id,
        tx_hash="ee" * 32,
        amount_atomic=parse_amount("0.05", ASSET_BTCX),
        block_height=1,
        asset=ASSET_BTCX,
    )
    # Spending entire BTCX with no PDC left for conversion → blocked.
    with pytest.raises(ValueError):
        svc.payout_milestone(
            admin, prop.id, amount="0.05", asset=ASSET_BTCX, milestone_note="m"
        )


def test_sendtoaddress_uses_decimal_string_not_float(tmp_path: Path):
    """Inspect payload construction without hitting real bitcoind."""
    settings = _settings(tmp_path, btc_rpc_url="http://127.0.0.1:18332")
    client = BtcWalletClient(settings)
    captured = {}

    def fake_call(method, params=None):
        captured["method"] = method
        captured["params"] = params
        return "txid_hex"

    client._call = fake_call  # type: ignore[method-assign]
    # 0.1 BTC exact in sats — float(0.1) is infamous; string must stay exact.
    client.send_to_address("bc1qtest", 10_000_000, comment="x")
    assert captured["method"] == "sendtoaddress"
    amount = captured["params"][1]
    assert isinstance(amount, str)
    assert amount == "0.10000000"


def test_wallet_rejected_releases_btcx_reservation(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Reject",
        content="x",
        category="misc",
        assets=[ASSET_PDC, ASSET_BTCX],
        targets={ASSET_PDC: "0.01", ASSET_BTCX: "0.05"},
        payout_addresses={ASSET_PDC: "pdcDest", ASSET_BTCX: "pdcDest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    pdc = svc.db.get_proposal_asset(prop.id, ASSET_PDC)
    row = svc.db.get_proposal_asset(prop.id, ASSET_BTCX)
    svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=pdc.payment_id,
        tx_hash="fe" * 32,
        amount_atomic=parse_amount("0.01", ASSET_PDC),
        block_height=1,
        asset=ASSET_PDC,
    )
    svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=row.payment_id,
        tx_hash="ff" * 32,
        amount_atomic=parse_amount("0.05", ASSET_BTCX),
        block_height=1,
        asset=ASSET_BTCX,
    )

    class Reject(WalletClient):
        @property
        def enabled(self) -> bool:
            return True

        def resolve_destination(self, address_or_alias: str) -> str:
            return address_or_alias

        def get_unlocked_atomic(self, asset_id=None):
            return 10**18

        def transfer(self, *a, **k):
            raise WalletRejected("node said no")

    svc.wallet = Reject(svc.settings)
    with pytest.raises(RuntimeError):
        svc.payout_milestone(
            admin, prop.id, amount="0.05", asset=ASSET_BTCX, milestone_note="m"
        )
    assert svc.available_atomic(prop, ASSET_BTCX) == parse_amount("0.05", ASSET_BTCX)
    assert svc.available_atomic(prop, ASSET_PDC) == parse_amount("0.01", ASSET_PDC)
    assert svc.db.list_payouts(prop.id) == []


def test_health_reports_btc_dry_run(tmp_path: Path):
    from fastapi.testclient import TestClient
    from funding.app import create_app

    settings = _settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["btc_wallet"] == "dry-run"
