from pathlib import Path

from funding.amounts import ATOMIC
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient


def test_progress_shows_remaining_after_partial_payout(tmp_path: Path):
    settings = Settings(
        secret_key="test",
        database_path=tmp_path / "escrow.db",
        wallet_rpc_url="",
        bootstrap_admin_password="",
        bootstrap_admin_user="admin",
    )
    db = Database(settings.database_path)
    svc = FundingService(settings, db, WalletClient(settings))
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="Partial",
        content="Work",
        category="misc",
        funds_target="10",
        payout_address="addr",
    )
    prop = svc.set_status(admin, prop.id, 2)
    assert prop.payment_id
    db.credit_deposit(
        proposal_id=prop.id,
        payment_id=prop.payment_id,
        tx_hash="tx1",
        amount_atomic=10 * ATOMIC,
        block_height=1,
    )
    svc.payout_milestone(admin, prop.id, amount="4", milestone_note="M1")
    prop = db.get_proposal(prop.id)
    assert prop is not None
    progress = svc.progress(prop)
    assert progress["received_atomic"] == 10 * ATOMIC
    assert progress["paid_atomic"] == 4 * ATOMIC
    assert progress["available_atomic"] == 6 * ATOMIC
    assert progress["paid_pct"] == 40.0
