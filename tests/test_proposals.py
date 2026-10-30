from pathlib import Path

from funding.amounts import ATOMIC
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient


def _svc(tmp_path: Path) -> FundingService:
    settings = Settings(
        secret_key="test",
        database_path=tmp_path / "t.db",
        wallet_rpc_url="",
        bootstrap_admin_password="",
        bootstrap_admin_user="admin",
    )
    db = Database(settings.database_path)
    return FundingService(settings, db, WalletClient(settings))


def test_register_create_fund_payout_dryrun(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    assert admin.is_admin

    author = svc.register("dev", "d@example.com", "password123")
    assert not author.is_admin

    proposal = svc.create_proposal(
        author,
        headline="Ship tipbot docs",
        content="#### Why?\nClarity.",
        category="docs",
        funds_target="10",
        payout_address="pdcSomeAddress",
    )
    assert proposal.status == 1
    assert proposal.funds_target_atomic == 10 * ATOMIC
    assert not proposal.payment_id

    funded = svc.set_status(admin, proposal.id, 2)
    assert funded.status == 2
    assert funded.payment_id
    assert funded.integrated_address.startswith("i")
    assert len(funded.payment_id) == 16

    # Simulate a donation credit
    ok = svc.db.credit_deposit(
        proposal_id=funded.id,
        payment_id=funded.payment_id,
        tx_hash="abc123",
        amount_atomic=10 * ATOMIC,
        block_height=100,
    )
    assert ok
    funded = svc.db.get_proposal(funded.id)
    assert funded is not None
    progress = svc.progress(funded)
    assert progress["pct"] == 100.0
    assert progress["available_atomic"] == 10 * ATOMIC

    result = svc.payout_milestone(
        admin,
        funded.id,
        amount="4",
        milestone_note="M1",
    )
    assert result["tx_hash"].startswith("dryrun_")
    funded = svc.db.get_proposal(funded.id)
    assert funded is not None
    progress = svc.progress(funded)
    assert progress["paid_atomic"] == 4 * ATOMIC
    assert progress["available_atomic"] == 6 * ATOMIC


def test_payout_cannot_overspend(tmp_path: Path):
    svc = _svc(tmp_path)
    admin = svc.register("admin", "a@example.com", "password123", force_admin=True)
    prop = svc.create_proposal(
        admin,
        headline="X",
        content="Y",
        category="misc",
        funds_target="1",
        payout_address="addr",
    )
    prop = svc.set_status(admin, prop.id, 2)
    svc.db.credit_deposit(
        proposal_id=prop.id,
        payment_id=prop.payment_id or "deadbeefdeadbeef",
        tx_hash="t1",
        amount_atomic=ATOMIC,
        block_height=1,
    )
    try:
        svc.payout_milestone(admin, prop.id, amount="2")
        assert False
    except ValueError as exc:
        assert "exceeds" in str(exc)
