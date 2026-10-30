import threading
from pathlib import Path

from fastapi.testclient import TestClient

from funding.amounts import ATOMIC
from funding.app import create_app
from funding.config import Settings
from funding.db import Database
from funding.service import FundingService
from funding.wallet import WalletClient, WalletError, WalletRejected
from tests.auth_helpers import TEST_TOTP_KEK, enable_totp, login_with_totp


def _funded(tmp_path: Path):
    settings = Settings(
        secret_key="test-secret",
        database_path=tmp_path / "pay.db",
        wallet_rpc_url="",
        bootstrap_admin_password="",
    )
    db = Database(settings.database_path)
    wallet = WalletClient(settings)
    svc = FundingService(settings, db, wallet)
    admin = svc.register("admin", "a@example.com", "password123")
    proposal = svc.create_proposal(
        admin,
        headline="Pay me",
        content="Work",
        category="misc",
        funds_target="10",
        payout_address="pdcSomeAddress",
    )
    proposal = svc.set_status(admin, proposal.id, 2)
    assert proposal.payment_id
    svc.db.credit_deposit(
        proposal_id=proposal.id,
        payment_id=proposal.payment_id,
        tx_hash="abc123",
        amount_atomic=5 * ATOMIC,
        block_height=10,
    )
    return settings, svc, admin, proposal


class SlowWallet(WalletClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transfer(self, address: str, amount_atomic: int, comment: str = "") -> str:
        self.calls += 1
        self.started.set()
        assert self.release.wait(5)
        return f"dryrun_{self.calls}"


class ScriptedWallet(WalletClient):
    def __init__(self, settings: Settings, exc: Exception) -> None:
        super().__init__(settings)
        self.exc = exc

    def resolve_destination(self, address_or_alias: str) -> str:
        return address_or_alias.strip()

    def transfer(self, address: str, amount_atomic: int, comment: str = "") -> str:
        raise self.exc


def test_concurrent_payout_cannot_double_spend(tmp_path: Path):
    _settings, svc, admin, proposal = _funded(tmp_path)
    slow = SlowWallet(svc.settings)
    svc.wallet = slow
    ok: list[dict] = []
    errors: list[BaseException] = []

    def pay() -> None:
        try:
            ok.append(svc.payout_milestone(admin, proposal.id, amount="5"))
        except BaseException as exc:  # noqa: BLE001 — thread boundary
            errors.append(exc)

    first = threading.Thread(target=pay)
    second = threading.Thread(target=pay)
    first.start()
    assert slow.started.wait(5)
    second.start()
    second.join(5)
    assert not second.is_alive()
    slow.release.set()
    first.join(5)
    assert not first.is_alive()
    assert slow.calls == 1
    assert len(ok) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "exceeds" in str(errors[0])
    refreshed = svc.db.get_proposal(proposal.id)
    assert refreshed is not None
    assert svc.progress(refreshed)["available_atomic"] == 0
    assert svc.progress(refreshed)["paid_atomic"] == 5 * ATOMIC


def test_rejected_transfer_releases_reservation(tmp_path: Path):
    _settings, svc, admin, proposal = _funded(tmp_path)
    svc.wallet = ScriptedWallet(svc.settings, WalletRejected("denied"))
    try:
        svc.payout_milestone(admin, proposal.id, amount="1")
        assert False
    except RuntimeError as exc:
        assert "denied" in str(exc)
    assert svc.db.list_payouts(proposal.id) == []
    refreshed = svc.db.get_proposal(proposal.id)
    assert refreshed is not None
    assert svc.progress(refreshed)["available_atomic"] == 5 * ATOMIC


def test_uncertain_transfer_keeps_reservation(tmp_path: Path):
    _settings, svc, admin, proposal = _funded(tmp_path)
    svc.wallet = ScriptedWallet(svc.settings, WalletError("timeout"))
    try:
        svc.payout_milestone(admin, proposal.id, amount="4")
        assert False
    except RuntimeError as exc:
        assert "reservation" in str(exc)
    payouts = svc.db.list_payouts(proposal.id)
    assert len(payouts) == 1
    assert payouts[0].tx_hash.startswith("pending:")
    try:
        svc.payout_milestone(admin, proposal.id, amount="2")
        assert False
    except ValueError as exc:
        assert "exceeds" in str(exc)


def test_donation_address_is_assigned_once(tmp_path: Path):
    settings = Settings(
        secret_key="test-secret",
        database_path=tmp_path / "addr.db",
        wallet_rpc_url="",
    )
    db = Database(settings.database_path)
    svc = FundingService(settings, db, WalletClient(settings))
    admin = svc.register("admin", "a@example.com", "password123")
    proposal = svc.create_proposal(
        admin,
        headline="Addr",
        content="Work",
        category="docs",
        funds_target="1",
    )
    assert db.set_donation_address_if_empty(proposal.id, "aa" * 8, "addr-one")
    assert not db.set_donation_address_if_empty(proposal.id, "bb" * 8, "addr-two")
    stored = db.get_proposal(proposal.id)
    assert stored is not None
    assert stored.payment_id == "aa" * 8
    assert stored.integrated_address == "addr-one"


def test_only_one_first_user_is_admin(tmp_path: Path):
    settings = Settings(
        secret_key="test-secret",
        database_path=tmp_path / "users.db",
        wallet_rpc_url="",
    )
    svc = FundingService(settings, Database(settings.database_path), WalletClient(settings))
    barrier = threading.Barrier(2)
    users = []
    errors = []

    def reg(name: str) -> None:
        barrier.wait(5)
        try:
            users.append(svc.register(name, f"{name}@example.com", "password123"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=reg, args=("alice",)),
        threading.Thread(target=reg, args=("bob",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors
    assert len(users) == 2
    assert sum(1 for user in users if user.is_admin) == 1


def test_password_over_bcrypt_limit(tmp_path: Path):
    settings = Settings(
        secret_key="test-secret",
        database_path=tmp_path / "pw.db",
        wallet_rpc_url="",
    )
    svc = FundingService(settings, Database(settings.database_path), WalletClient(settings))
    try:
        svc.register("admin", "a@example.com", "x" * 73)
        assert False
    except ValueError as exc:
        assert "72" in str(exc)


def test_pending_payout_label(tmp_path: Path):
    settings = Settings(
        secret_key="test-secret",
        database_path=tmp_path / "ui.db",
        wallet_rpc_url="",
        bootstrap_admin_password="password123",
        bootstrap_admin_user="admin",
        bootstrap_admin_email="admin@example.com",
        funding_totp_kek=TEST_TOTP_KEK,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        svc = app.state.service
        admin = svc.db.get_user_by_username("admin")
        assert admin is not None
        enable_totp(svc, admin.id)
        proposal = svc.create_proposal(
            admin,
            headline="UI",
            content="Work",
            category="misc",
            funds_target="2",
            payout_address="addr",
        )
        proposal = svc.set_status(admin, proposal.id, 2)
        assert proposal.payment_id
        svc.db.credit_deposit(
            proposal_id=proposal.id,
            payment_id=proposal.payment_id,
            tx_hash="abc123",
            amount_atomic=ATOMIC,
            block_height=3,
        )
        svc.db.reserve_payout(
            proposal_id=proposal.id,
            amount_atomic=ATOMIC,
            to_address="addr",
            milestone_note="held",
            created_by=admin.id,
        )
        login_with_totp(client, "admin", "password123")
        page = client.get(f"/proposals/{proposal.id}")
        assert page.status_code == 200
        assert b"pending" in page.content
        assert b"pending:" not in page.content
