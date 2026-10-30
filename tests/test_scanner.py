from pathlib import Path

from funding.amounts import ATOMIC
from funding.config import Settings
from funding.db import Database
from funding.scanner import META_WATERMARK, DepositScanner
from funding.service import FundingService
from funding.wallet import (
    Payment,
    WalletClient,
    parse_chain_height,
    payment_is_unlocked,
)


class StubWallet(WalletClient):
    def __init__(self, settings: Settings, payments: list[Payment], height: int) -> None:
        super().__init__(settings)
        self.payments = payments
        self.height = height
        self.queries: list[int] = []

    def get_height(self) -> int:
        return self.height

    def get_bulk_payments(self, payment_ids, min_block_height=0, *, allow_locked=False):
        self.queries.append(min_block_height)
        ids = {p.lower() for p in payment_ids}
        return [
            p
            for p in self.payments
            if p.payment_id.lower() in ids and p.block_height >= min_block_height
        ]


def _open(tmp_path: Path, *, min_conf: int = 10):
    path = tmp_path / "scan.db"
    dry = Settings(
        secret_key="test-secret",
        database_path=path,
        wallet_rpc_url="",
        bootstrap_admin_password="",
        bootstrap_admin_user="admin",
    )
    db = Database(path)
    svc = FundingService(dry, db, WalletClient(dry))
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    proposal = svc.create_proposal(
        admin,
        headline="Scan me",
        content="Work",
        category="misc",
        funds_target="10",
        payout_address="addr",
    )
    proposal = svc.set_status(admin, proposal.id, 2)
    live = Settings(
        secret_key="test-secret",
        database_path=path,
        wallet_rpc_url="http://127.0.0.1:9/json_rpc",
        deposit_min_confirmations=min_conf,
    )
    assert proposal.payment_id
    return db, proposal, live


def _pay(proposal, **kwargs) -> Payment:
    base = dict(
        payment_id=proposal.payment_id,
        tx_hash="ab" * 32,
        amount=ATOMIC,
        block_height=90,
        unlock_time=0,
        asset_id="",
    )
    base.update(kwargs)
    return Payment(**base)


def test_confirmation_window_and_later_credit(tmp_path: Path):
    db, proposal, live = _open(tmp_path, min_conf=10)
    payment = _pay(proposal, block_height=91)
    wallet = StubWallet(live, [payment], height=100)
    scanner = DepositScanner(live, db, wallet)
    assert scanner.scan_once() == 0
    assert db.list_deposits(proposal.id) == []
    mark = int(db.get_meta(META_WATERMARK))
    assert mark <= 90
    wallet.height = 110
    assert scanner.scan_once() == 1
    assert db.list_deposits(proposal.id)[0].amount_atomic == ATOMIC


def test_height_unknown_does_not_credit(tmp_path: Path):
    db, proposal, live = _open(tmp_path)
    wallet = StubWallet(live, [_pay(proposal, block_height=90)], height=0)
    scanner = DepositScanner(live, db, wallet)
    assert scanner.scan_once() == 0
    assert wallet.queries == []
    assert db.list_deposits(proposal.id) == []


def test_mempool_payment_is_not_credited(tmp_path: Path):
    db, proposal, live = _open(tmp_path)
    wallet = StubWallet(live, [_pay(proposal, block_height=0)], height=100)
    scanner = DepositScanner(live, db, wallet)
    assert scanner.scan_once() == 0
    assert db.list_deposits(proposal.id) == []


def test_timestamp_lock_holds_watermark(tmp_path: Path):
    db, proposal, live = _open(tmp_path)
    locked = _pay(proposal, block_height=10, unlock_time=4_000_000_000)
    wallet = StubWallet(live, [locked], height=1000)
    scanner = DepositScanner(live, db, wallet)
    assert scanner.scan_once() == 0
    assert int(db.get_meta(META_WATERMARK)) <= 9
    assert scanner.scan_once() == 0
    wallet.payments = [_pay(proposal, block_height=10, unlock_time=1_700_000_000)]
    assert scanner.scan_once() == 1


def test_wrong_asset_is_dropped(tmp_path: Path):
    db, proposal, live = _open(tmp_path)
    wallet = StubWallet(
        live,
        [_pay(proposal, block_height=20, asset_id="deadbeef")],
        height=100,
    )
    scanner = DepositScanner(live, db, wallet)
    assert scanner.scan_once() == 0
    assert db.list_deposits(proposal.id) == []
    assert int(db.get_meta(META_WATERMARK)) == 85


def test_tx_hash_case_is_one_credit(tmp_path: Path):
    db, proposal, _live = _open(tmp_path)
    assert db.credit_deposit(
        proposal_id=proposal.id,
        payment_id=proposal.payment_id or "",
        tx_hash="ABC",
        amount_atomic=1,
        block_height=1,
    )
    assert not db.credit_deposit(
        proposal_id=proposal.id,
        payment_id=proposal.payment_id or "",
        tx_hash="abc",
        amount_atomic=1,
        block_height=1,
    )
    assert len(db.list_deposits(proposal.id)) == 1


def test_chain_height_and_unlock_rules():
    assert parse_chain_height({"height": 12}) == 12
    assert parse_chain_height({"result": {"height": 9}}) == 9
    assert parse_chain_height({"height": 0}) == 0
    assert parse_chain_height({"height": -3}) == 0
    assert parse_chain_height(None) == 0
    assert payment_is_unlocked(0, 10)
    assert payment_is_unlocked(1_700_000_000, 10, now=1_800_000_000)
    assert not payment_is_unlocked(4_000_000_000, 10_000, now=1_800_000_000)
    assert not payment_is_unlocked(50, 40)
    assert payment_is_unlocked(50, 50)
    assert not payment_is_unlocked(50, 0)


def test_bulk_payments_skip_malformed(tmp_path: Path):
    settings = Settings(
        secret_key="test-secret",
        database_path=tmp_path / "w.db",
        wallet_rpc_url="http://127.0.0.1:9/json_rpc",
    )
    wallet = WalletClient(settings)
    pid = "ab" * 8

    def fake_call(method, params=None):
        assert method == "get_bulk_payments"
        return {
            "payments": [
                {
                    "payment_id": pid.upper(),
                    "tx_hash": "AA",
                    "amount": 5,
                    "block_height": 3,
                    "asset_id": "ASSET",
                },
                {"amount": 1},
                "nope",
            ]
        }

    wallet._call = fake_call  # type: ignore[method-assign]
    pays = wallet.get_bulk_payments([pid])
    assert len(pays) == 1
    assert pays[0].asset_id == "asset"
    assert pays[0].payment_id == pid
    assert pays[0].amount == 5
