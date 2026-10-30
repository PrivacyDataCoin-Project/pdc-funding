from pathlib import Path

from funding.amounts import ATOMIC
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient


class CaptureWallet(WalletClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.last_amount: int | None = None
        self.last_address: str | None = None

    def resolve_destination(self, address_or_alias: str) -> str:
        return address_or_alias.strip()

    def transfer(self, address: str, amount_atomic: int, comment: str = "") -> str:
        self.last_address = address
        self.last_amount = amount_atomic
        return "tx_fee_test"


def _svc(tmp_path: Path, deposit: str = "10"):
    settings = Settings(
        secret_key="test",
        database_path=tmp_path / "fee.db",
        wallet_rpc_url="",
        bootstrap_admin_password="",
        transfer_fee_atomic=10_000_000_000,  # 0.01 PDC
    )
    db = Database(settings.database_path)
    wallet = CaptureWallet(settings)
    svc = FundingService(settings, db, wallet)
    admin = svc.register("admin", "a@example.com", "password123")
    prop = svc.create_proposal(
        admin,
        headline="Fee",
        content="Work",
        category="misc",
        funds_target="10",
        payout_address="pdcDest",
    )
    prop = svc.set_status(admin, prop.id, 2)
    assert prop.payment_id
    db.credit_deposit(
        proposal_id=prop.id,
        payment_id=prop.payment_id,
        tx_hash="dep1",
        amount_atomic=parse_deposit(deposit),
        block_height=1,
    )
    return svc, admin, prop, wallet


def parse_deposit(text: str) -> int:
    from funding.amounts import parse_amount

    return parse_amount(text)


def test_payout_sends_amount_minus_fee(tmp_path: Path):
    svc, admin, prop, wallet = _svc(tmp_path)
    result = svc.payout_milestone(admin, prop.id, amount="4")
    assert result["amount_atomic"] == 4 * ATOMIC
    assert result["fee_atomic"] == 10_000_000_000
    assert result["send_atomic"] == 4 * ATOMIC - 10_000_000_000
    assert wallet.last_amount == result["send_atomic"]
    assert wallet.last_address == "pdcDest"
    refreshed = svc.db.get_proposal(prop.id)
    assert refreshed is not None
    assert svc.progress(refreshed)["available_atomic"] == 6 * ATOMIC
    assert svc.progress(refreshed)["paid_atomic"] == 4 * ATOMIC


def test_full_payout_leaves_zero_after_fee(tmp_path: Path):
    svc, admin, prop, wallet = _svc(tmp_path)
    result = svc.payout_milestone(admin, prop.id, amount="10")
    assert wallet.last_amount == 10 * ATOMIC - 10_000_000_000
    assert result["send_atomic"] == wallet.last_amount
    refreshed = svc.db.get_proposal(prop.id)
    assert refreshed is not None
    assert svc.progress(refreshed)["available_atomic"] == 0


def test_payout_rejects_amount_not_above_fee(tmp_path: Path):
    svc, admin, prop, _wallet = _svc(tmp_path)
    try:
        svc.payout_milestone(admin, prop.id, amount="0.01")
        assert False
    except ValueError as exc:
        assert "fee" in str(exc).lower()
    assert svc.db.list_payouts(prop.id) == []
