"""Multi-asset proposals (PDC / BTCX / BTC) — dry-run, no real spends."""

from pathlib import Path

from funding.assets import ASSET_BTC, ASSET_BTCX, ASSET_PDC, format_amount, parse_amount
from funding.btc_wallet import BtcWalletClient
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient


def _svc(tmp_path: Path) -> FundingService:
    settings = Settings(
        secret_key="test",
        database_path=tmp_path / "multi.db",
        wallet_rpc_url="",
        btc_rpc_url="",
        bootstrap_admin_password="",
        bootstrap_admin_user="admin",
    )
    db = Database(settings.database_path)
    return FundingService(settings, db, WalletClient(settings), BtcWalletClient(settings))


def test_parse_format_per_asset():
    assert parse_amount("1", ASSET_PDC) == 10**12
    assert parse_amount("0.00000001", ASSET_BTC) == 1
    assert parse_amount("1.5", ASSET_BTCX) == 150_000_000
    assert format_amount(10**12, ASSET_PDC) == "1.000"
    assert format_amount(1, ASSET_BTC) == "0.00000001"


def test_create_multi_asset_and_assign_addresses(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Multi",
        content="Work",
        category="misc",
        assets=[ASSET_PDC, ASSET_BTCX, ASSET_BTC],
        targets={
            ASSET_PDC: "10",
            ASSET_BTCX: "0.01",
            ASSET_BTC: "0.001",
        },
        payout_addresses={
            ASSET_PDC: "pdcAddr",
            ASSET_BTCX: "pdcAddr",
            ASSET_BTC: "bc1qtest",
        },
    )
    rows = svc.db.list_proposal_assets(prop.id)
    tickers = {r.asset for r in rows}
    assert tickers == {ASSET_PDC, ASSET_BTCX, ASSET_BTC}

    prop = svc.set_status(admin, prop.id, 2)
    rows = svc.db.list_proposal_assets(prop.id)
    by = {r.asset: r for r in rows}
    # Shared L1 integrated address for PDC + BTCX
    assert by[ASSET_PDC].integrated_address
    assert by[ASSET_BTCX].integrated_address
    assert by[ASSET_PDC].payment_id == by[ASSET_BTCX].payment_id
    assert by[ASSET_PDC].integrated_address == by[ASSET_BTCX].integrated_address
    # BTC dry-run address assigned
    assert by[ASSET_BTC].btc_address.startswith("bcrt1q")

    progress = svc.progress(prop)
    assert len(progress["assets"]) == 3


def test_credit_and_payout_per_asset_dryrun(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Pay",
        content="Work",
        category="misc",
        assets=[ASSET_PDC, ASSET_BTC],
        targets={ASSET_PDC: "5", ASSET_BTC: "0.01"},
        payout_addresses={ASSET_PDC: "pdcDest", ASSET_BTC: "bc1qdest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    pdc = svc.db.get_proposal_asset(prop.id, ASSET_PDC)
    btc = svc.db.get_proposal_asset(prop.id, ASSET_BTC)
    assert pdc and pdc.payment_id and btc and btc.btc_address

    assert svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=pdc.payment_id,
        tx_hash="aa" * 32,
        amount_atomic=parse_amount("5", ASSET_PDC),
        block_height=10,
        asset=ASSET_PDC,
    )
    assert svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=btc.btc_address.lower(),
        tx_hash="btc:batch1",
        amount_atomic=parse_amount("0.01", ASSET_BTC),
        block_height=100,
        asset=ASSET_BTC,
    )
    # Wrong asset on same payment_id must not mix balances.
    pdc_prog = svc.db.get_proposal_asset(prop.id, ASSET_PDC)
    btc_prog = svc.db.get_proposal_asset(prop.id, ASSET_BTC)
    assert pdc_prog.received_atomic == parse_amount("5", ASSET_PDC)
    assert btc_prog.received_atomic == parse_amount("0.01", ASSET_BTC)

    out_pdc = svc.payout_milestone(
        admin, prop.id, amount="5", asset=ASSET_PDC, milestone_note="M1"
    )
    assert out_pdc["asset"] == ASSET_PDC
    assert out_pdc["tx_hash"].startswith("dryrun_")

    out_btc = svc.payout_milestone(
        admin, prop.id, amount="0.01", asset=ASSET_BTC, milestone_note="M1b"
    )
    assert out_btc["asset"] == ASSET_BTC
    assert out_btc["tx_hash"].startswith("btc_dryrun_")

    assert svc.available_atomic(prop, ASSET_PDC) == 0
    assert svc.available_atomic(prop, ASSET_BTC) == 0


def test_btcx_only_proposal(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="BTCX only",
        content="Work",
        category="core",
        assets=[ASSET_BTCX],
        targets={ASSET_BTCX: "0.05"},
        payout_addresses={ASSET_BTCX: "pdcDest"},
    )
    rows = svc.db.list_proposal_assets(prop.id)
    # Autonomous fee model auto-adds a PDC lane for L1 network fees.
    assert {r.asset for r in rows} == {ASSET_PDC, ASSET_BTCX}
    prop = svc.set_status(admin, prop.id, 2)
    row = svc.db.get_proposal_asset(prop.id, ASSET_BTCX)
    assert row and row.integrated_address
    pdc = svc.db.get_proposal_asset(prop.id, ASSET_PDC)
    assert pdc and pdc.payment_id == row.payment_id


def test_btcx_payout_debits_pdc_escrow_for_l1_fee(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="BTCX fee escrow",
        content="Work",
        category="misc",
        assets=[ASSET_PDC, ASSET_BTCX],
        targets={ASSET_PDC: "0.01", ASSET_BTCX: "0.05"},
        payout_addresses={ASSET_PDC: "pdcDest", ASSET_BTCX: "pdcDest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    pdc = svc.db.get_proposal_asset(prop.id, ASSET_PDC)
    btcx = svc.db.get_proposal_asset(prop.id, ASSET_BTCX)
    assert svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=pdc.payment_id,
        tx_hash="aa" * 32,
        amount_atomic=parse_amount("0.01", ASSET_PDC),
        block_height=1,
        asset=ASSET_PDC,
    )
    assert svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=btcx.payment_id,
        tx_hash="bb" * 32,
        amount_atomic=parse_amount("0.05", ASSET_BTCX),
        block_height=1,
        asset=ASSET_BTCX,
    )
    out = svc.payout_milestone(
        admin, prop.id, amount="0.05", asset=ASSET_BTCX, milestone_note="M1"
    )
    assert out["asset"] == ASSET_BTCX
    assert out["send_atomic"] == parse_amount("0.05", ASSET_BTCX)
    assert out["fee_atomic"] == svc.settings.transfer_fee_atomic
    assert svc.available_atomic(prop, ASSET_BTCX) == 0
    assert svc.available_atomic(prop, ASSET_PDC) == 0
    pays = svc.db.list_payouts(prop.id)
    assert len(pays) == 2
    assert {p.asset for p in pays} == {ASSET_PDC, ASSET_BTCX}


def test_btcx_payout_blocked_without_fee_cover(tmp_path: Path):
    """Spending all BTCX with zero PDC leaves nothing to convert for L1 fee."""
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="No fee cover",
        content="Work",
        category="misc",
        assets=[ASSET_BTCX],
        targets={ASSET_BTCX: "0.05"},
        payout_addresses={ASSET_BTCX: "pdcDest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    btcx = svc.db.get_proposal_asset(prop.id, ASSET_BTCX)
    svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=btcx.payment_id,
        tx_hash="cc" * 32,
        amount_atomic=parse_amount("0.05", ASSET_BTCX),
        block_height=1,
        asset=ASSET_BTCX,
    )
    try:
        svc.payout_milestone(
            admin, prop.id, amount="0.05", asset=ASSET_BTCX, milestone_note="M1"
        )
        assert False
    except ValueError as exc:
        msg = str(exc).lower()
        assert "fee" in msg or "escrow" in msg or "insufficient" in msg


def test_btcx_payout_converts_leftover_btcx_for_fee(tmp_path: Path, monkeypatch):
    """When PDC escrow is empty, leftover BTCX covers L1 fee at oracle rate."""
    from funding import prices as prices_mod
    from funding.prices import PriceBook

    monkeypatch.setattr(
        prices_mod,
        "fetch_prices",
        lambda settings, force=False: PriceBook(
            updated_at=1.0,
            prices={"PDC": 1.0, "BTCX": 50_000.0, "BTC": 50_000.0},
            source="test",
        ),
    )
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Convert fee",
        content="Work",
        category="misc",
        assets=[ASSET_BTCX],
        targets={ASSET_BTCX: "1"},
        payout_addresses={ASSET_BTCX: "pdcDest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    btcx = svc.db.get_proposal_asset(prop.id, ASSET_BTCX)
    # Fund 1 BTCX: pay out 0.5, leave 0.5 for fee conversion (>> 0.01 PDC).
    svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=btcx.payment_id,
        tx_hash="dd" * 32,
        amount_atomic=parse_amount("1", ASSET_BTCX),
        block_height=1,
        asset=ASSET_BTCX,
    )
    out = svc.payout_milestone(
        admin, prop.id, amount="0.5", asset=ASSET_BTCX, milestone_note="M1"
    )
    assert out["fee_cover"] is not None
    assert out["fee_cover"]["pdc"] == 0
    assert out["fee_cover"]["btcx"] > 0
    assert svc.available_atomic(prop, ASSET_BTCX) == (
        parse_amount("1", ASSET_BTCX)
        - parse_amount("0.5", ASSET_BTCX)
        - out["fee_cover"]["btcx"]
    )


def test_btc_cumulative_credit_never_double_counts(tmp_path: Path):
    """Same chain total must not credit twice even if height changes."""
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="BTC cum",
        content="Work",
        category="misc",
        assets=[ASSET_BTC],
        targets={ASSET_BTC: "0.01"},
        payout_addresses={ASSET_BTC: "bc1qdest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    addr = svc.db.get_proposal_asset(prop.id, ASSET_BTC).btc_address
    assert svc.db.credit_btc_cumulative(
        proposal_id=prop.id,
        btc_address=addr,
        chain_received_sats=100_000,
        block_height=800_000,
    ) == 100_000
    # Same cumulative, different height → 0
    assert (
        svc.db.credit_btc_cumulative(
            proposal_id=prop.id,
            btc_address=addr,
            chain_received_sats=100_000,
            block_height=800_001,
        )
        == 0
    )
    # New funds → only the delta
    assert (
        svc.db.credit_btc_cumulative(
            proposal_id=prop.id,
            btc_address=addr,
            chain_received_sats=150_000,
            block_height=800_002,
        )
        == 50_000
    )
    row = svc.db.get_proposal_asset(prop.id, ASSET_BTC)
    assert row.received_atomic == 150_000


def test_empty_asset_id_not_credited_on_pdc_btcx_proposal(tmp_path: Path):
    """Ambiguous native payment must not inflate PDC when BTCX shares the PID."""
    from funding.scanner import DepositScanner
    from funding.wallet import Payment, WalletClient

    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Ambiguous",
        content="Work",
        category="misc",
        assets=[ASSET_PDC, ASSET_BTCX],
        targets={ASSET_PDC: "1", ASSET_BTCX: "0.01"},
        payout_addresses={ASSET_PDC: "pdcDest", ASSET_BTCX: "pdcDest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    pid = svc.db.get_proposal_asset(prop.id, ASSET_PDC).payment_id

    class Stub(WalletClient):
        @property
        def enabled(self) -> bool:
            return True

        def get_height(self) -> int:
            return 200

        def get_bulk_payments(self, payment_ids, min_block_height=0, **kw):
            return [
                Payment(
                    payment_id=pid,
                    tx_hash="cc" * 32,
                    amount=parse_amount("1", ASSET_PDC),
                    block_height=100,
                    unlock_time=0,
                    asset_id="",
                )
            ]

    live = Settings(
        secret_key="test",
        database_path=tmp_path / "multi.db",
        wallet_rpc_url="http://127.0.0.1:9/json_rpc",
        deposit_min_confirmations=10,
        btc_rpc_url="",
    )
    scanner = DepositScanner(live, svc.db, Stub(live), BtcWalletClient(live))
    assert scanner.scan_once() == 0
    assert svc.db.get_proposal_asset(prop.id, ASSET_PDC).received_atomic == 0
    assert svc.db.get_proposal_asset(prop.id, ASSET_BTCX).received_atomic == 0


def test_l1_scanner_splits_pdc_and_btcx_by_asset_id(tmp_path: Path):
    from funding.scanner import DepositScanner
    from funding.wallet import Payment, WalletClient

    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="L1 split",
        content="Work",
        category="misc",
        assets=[ASSET_PDC, ASSET_BTCX],
        targets={ASSET_PDC: "1", ASSET_BTCX: "0.01"},
        payout_addresses={ASSET_PDC: "pdcDest", ASSET_BTCX: "pdcDest"},
    )
    prop = svc.set_status(admin, prop.id, 2)
    pid = svc.db.get_proposal_asset(prop.id, ASSET_PDC).payment_id
    pdc_id = svc.settings.pdc_asset_id
    btcx_id = svc.settings.btcx_asset_id

    class Stub(WalletClient):
        def __init__(self, settings):
            super().__init__(settings)
            self._payments = [
                Payment(
                    payment_id=pid,
                    tx_hash="aa" * 32,
                    amount=parse_amount("1", ASSET_PDC),
                    block_height=100,
                    unlock_time=0,
                    asset_id=pdc_id,
                ),
                Payment(
                    payment_id=pid,
                    tx_hash="bb" * 32,
                    amount=parse_amount("0.01", ASSET_BTCX),
                    block_height=100,
                    unlock_time=0,
                    asset_id=btcx_id,
                ),
            ]

        @property
        def enabled(self) -> bool:
            return True

        def get_height(self) -> int:
            return 200

        def get_bulk_payments(self, payment_ids, min_block_height=0, **kw):
            return list(self._payments)

    live = Settings(
        secret_key="test",
        database_path=tmp_path / "multi.db",
        wallet_rpc_url="http://127.0.0.1:9/json_rpc",
        deposit_min_confirmations=10,
        btc_rpc_url="",
    )
    scanner = DepositScanner(live, svc.db, Stub(live), BtcWalletClient(live))
    assert scanner.scan_once() == 2
    pdc = svc.db.get_proposal_asset(prop.id, ASSET_PDC)
    btcx = svc.db.get_proposal_asset(prop.id, ASSET_BTCX)
    assert pdc.received_atomic == parse_amount("1", ASSET_PDC)
    assert btcx.received_atomic == parse_amount("0.01", ASSET_BTCX)
    # Wrong asset must not land on the other ledger
    assert pdc.received_atomic != btcx.received_atomic
