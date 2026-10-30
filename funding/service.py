"""Domain logic for PDC Funding."""

from __future__ import annotations

import logging
import re
import secrets
from typing import Optional

import bcrypt

from funding.amounts import format_amount, funding_pct, parse_amount
from funding.config import CATEGORIES, STATUSES, Settings
from funding.db import Comment, Database, Proposal, User
from funding.totp import (
    decrypt_secret,
    derive_kek,
    encrypt_secret,
    new_secret_base32,
    provisioning_uri,
    qr_data_url,
    verify_code,
)
from funding.wallet import WalletClient, WalletError, WalletRejected

logger = logging.getLogger("pdc-funding.service")

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
_BCRYPT_MAX_BYTES = 72
_MAX_CONTENT_CHARS = 100_000
_MAX_NOTE_CHARS = 4_000
_MAX_ADDRESS_CHARS = 1_024
# Checked when the username does not exist so missing users are not faster.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    b"pdc-funding-dummy", bcrypt.gensalt()
).decode("utf-8")
PROPOSAL_TEMPLATE = """#### Why?

What problem(s) are you trying to solve?

#### How much?

Total cost in PDC. List expenses. Hours and rate. Which USD/PDC reference?

#### What?

Describe the work in detail.

#### Milestones?

Stages with estimated time and PDC per stage.

#### Outcomes?

What will be delivered?

#### Why you?

Skills and experience.
""".strip()


class FundingService:
    def __init__(self, settings: Settings, db: Database, wallet: WalletClient) -> None:
        self.settings = settings
        self.db = db
        self.wallet = wallet
        self._totp_kek = derive_kek(settings.funding_totp_kek)

    def bootstrap_admin(self) -> None:
        if self.db.user_count() > 0:
            return
        password = self.settings.bootstrap_admin_password
        if not password:
            logger.warning(
                "no users and BOOTSTRAP_ADMIN_PASSWORD empty — register first user manually"
            )
            return
        self.register(
            self.settings.bootstrap_admin_user,
            self.settings.bootstrap_admin_email,
            password,
            force_admin=True,
        )
        logger.info("bootstrap admin user created: %s", self.settings.bootstrap_admin_user)

    def hash_password(self, password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"), password_hash.encode("utf-8")
            )
        except ValueError:
            return False

    def register(
        self,
        username: str,
        email: str,
        password: str,
        *,
        force_admin: bool = False,
    ) -> User:
        if not _USERNAME_RE.match(username or ""):
            raise ValueError("username must be 3–20 chars [A-Za-z0-9_]")
        email_norm = (email or "").strip()
        if "@" not in email_norm or not email_norm.split("@")[0]:
            raise ValueError("invalid email")
        domain = email_norm.split("@", 1)[1]
        if not domain or " " in domain:
            raise ValueError("invalid email")
        if len(password or "") < 8:
            raise ValueError("password must be at least 8 characters")
        if len((password or "").encode("utf-8")) > _BCRYPT_MAX_BYTES:
            raise ValueError("password must be at most 72 bytes")
        # When BOOTSTRAP_ADMIN_PASSWORD is set, never auto-promote the first
        # public registrant — only bootstrap / force_admin creates an admin.
        allow_promote = not force_admin and not bool(
            (self.settings.bootstrap_admin_password or "").strip()
        )
        return self.db.create_user(
            username,
            email,
            self.hash_password(password),
            is_admin=bool(force_admin),
            promote_if_first=allow_promote,
        )

    def require_primary_admin(self, actor: User) -> None:
        """Privileged money/status/user ops: bootstrap admin account only."""
        if not actor.is_admin:
            raise PermissionError("admin required")
        expected = (self.settings.bootstrap_admin_user or "admin").strip().lower()
        if actor.username.lower() != expected:
            raise PermissionError("admin required")

    def authenticate(self, username: str, password: str) -> Optional[User]:
        user = self.db.get_user_by_username(username)
        if not user:
            self.verify_password(password, _DUMMY_PASSWORD_HASH)
            return None
        if not self.verify_password(password, user.password_hash):
            return None
        return user

    def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> None:
        if not self.verify_password(current_password, user.password_hash):
            raise ValueError("current password is incorrect")
        if len(new_password or "") < 8:
            raise ValueError("password must be at least 8 characters")
        if len((new_password or "").encode("utf-8")) > _BCRYPT_MAX_BYTES:
            raise ValueError("password must be at most 72 bytes")
        if current_password == new_password:
            raise ValueError("new password must differ from the current one")
        self.db.set_password_hash(user.id, self.hash_password(new_password))

    def admin_reset_password(self, actor: User, user_id: int) -> str:
        """Set a random password for another user. Returns the plaintext once."""
        self.require_primary_admin(actor)
        target = self.db.get_user(user_id)
        if not target:
            raise KeyError("user not found")
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        new_password = "".join(secrets.choice(alphabet) for _ in range(16))
        self.db.set_password_hash(target.id, self.hash_password(new_password))
        return new_password

    def admin_reset_totp(self, actor: User, user_id: int) -> None:
        self.require_primary_admin(actor)
        if not self.db.get_user(user_id):
            raise KeyError("user not found")
        self.db.clear_totp(user_id)

    def begin_totp_enroll(self, user: User) -> dict:
        """Store a new encrypted secret (enabled=0) and return QR material."""
        secret = new_secret_base32()
        enc = encrypt_secret(secret, self._totp_kek)
        self.db.set_totp_secret(user.id, enc, enabled=False)
        uri = provisioning_uri(
            secret, username=user.username, issuer=self.settings.totp_issuer
        )
        return {
            "secret_base32": secret,
            "totp_uri": uri,
            "qr_data_url": qr_data_url(uri),
        }

    def confirm_totp_enroll(self, user: User, code: str) -> User:
        blob = self.db.get_totp_secret_enc(user.id)
        if not blob:
            raise ValueError("start 2FA enrollment first")
        secret = decrypt_secret(blob, self._totp_kek)
        if not verify_code(secret, code):
            raise ValueError("invalid authenticator code")
        self.db.enable_totp(user.id)
        refreshed = self.db.get_user(user.id)
        assert refreshed is not None
        return refreshed

    def start_login_totp_challenge(self, user: User) -> str:
        if not user.totp_enabled:
            raise ValueError("2FA is not enabled for this account")
        return self.db.create_login_challenge(user.id, "login", ttl_seconds=300)

    def finish_login_totp(self, challenge_id: str, code: str) -> User:
        # Verify TOTP before consuming so a mistyped code can be retried.
        user_id = self.db.peek_login_challenge(challenge_id, purpose="login")
        if user_id is None:
            raise ValueError("login challenge expired or invalid")
        user = self.db.get_user(user_id)
        if not user or not user.totp_enabled:
            raise ValueError("user unavailable")
        blob = self.db.get_totp_secret_enc(user.id)
        if not blob:
            raise ValueError("2FA secret missing")
        secret = decrypt_secret(blob, self._totp_kek)
        if not verify_code(secret, code):
            raise ValueError("invalid authenticator code")
        consumed = self.db.consume_login_challenge(challenge_id, purpose="login")
        if consumed is None or consumed != user.id:
            raise ValueError("login challenge expired or invalid")
        return user

    def create_proposal(
        self,
        user: User,
        *,
        headline: str,
        content: str,
        category: str,
        funds_target: str,
        payout_address: str = "",
    ) -> Proposal:
        if not headline.strip() or len(headline.strip()) > 200:
            raise ValueError("headline required (max 200 chars)")
        if not content.strip():
            raise ValueError("content required")
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError("content too long")
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(CATEGORIES)}")
        if len((payout_address or "").strip()) > _MAX_ADDRESS_CHARS:
            raise ValueError("payout address too long")
        target = parse_amount(funds_target)
        return self.db.create_proposal(
            user_id=user.id,
            headline=headline,
            content=content,
            category=category,
            funds_target_atomic=target,
            payout_address=payout_address,
            status=1,
        )

    def ensure_donation_address(self, proposal: Proposal) -> Proposal:
        """Assign payment_id + integrated address when proposal enters funding."""
        if proposal.payment_id and proposal.integrated_address:
            return proposal
        if proposal.status < 2:
            return proposal
        address, pid = self.wallet.make_integrated_address()
        if not self.db.set_donation_address_if_empty(proposal.id, pid, address):
            refreshed = self.db.get_proposal(proposal.id)
            if refreshed and refreshed.payment_id and refreshed.integrated_address:
                return refreshed
            raise RuntimeError("could not assign donation address")
        refreshed = self.db.get_proposal(proposal.id)
        if not refreshed:
            raise RuntimeError("proposal vanished after address assign")
        return refreshed

    def set_status(self, actor: User, proposal_id: int, status: int) -> Proposal:
        self.require_primary_admin(actor)
        if status not in STATUSES:
            raise ValueError("invalid status")
        proposal = self.db.get_proposal(proposal_id)
        if not proposal:
            raise KeyError("proposal not found")
        self.db.update_proposal_status(proposal_id, status)
        proposal = self.db.get_proposal(proposal_id)
        assert proposal is not None
        if status >= 2:
            proposal = self.ensure_donation_address(proposal)
        return proposal

    def available_atomic(self, proposal: Proposal) -> int:
        return max(0, int(proposal.received_atomic) - int(proposal.paid_atomic))

    def progress(self, proposal: Proposal) -> dict:
        received = int(proposal.received_atomic)
        paid = int(proposal.paid_atomic)
        pending = int(getattr(proposal, "pending_atomic", 0) or 0)
        available = max(0, received - paid)
        return {
            "received_atomic": received,
            "paid_atomic": paid,
            "pending_atomic": pending,
            "available_atomic": available,
            "target_atomic": int(proposal.funds_target_atomic),
            "pct": funding_pct(received, int(proposal.funds_target_atomic)),
            "paid_pct": funding_pct(paid, received) if received > 0 else 0.0,
        }

    def payout_milestone(
        self,
        actor: User,
        proposal_id: int,
        *,
        amount: str,
        to_address: Optional[str] = None,
        milestone_note: str = "",
    ) -> dict:
        self.require_primary_admin(actor)
        proposal = self.db.get_proposal(proposal_id)
        if not proposal:
            raise KeyError("proposal not found")
        if proposal.status not in (2, 3, 4):
            raise ValueError("proposal must be funding/wip/completed to pay out")
        note = (milestone_note or "").strip()
        if len(note) > _MAX_NOTE_CHARS:
            raise ValueError("note too long")
        amount_atomic = parse_amount(amount)
        fee_atomic = int(self.settings.transfer_fee_atomic)
        if amount_atomic <= fee_atomic:
            raise ValueError(
                f"amount must be greater than transfer fee "
                f"({format_amount(fee_atomic)} PDC)"
            )
        send_atomic = amount_atomic - fee_atomic
        dest = (to_address or proposal.payout_address or "").strip()
        if not dest:
            raise ValueError("payout address required")
        if len(dest) > _MAX_ADDRESS_CHARS:
            raise ValueError("payout address too long")
        if self.wallet.enabled:
            try:
                dest = self.wallet.resolve_destination(dest)
            except WalletError as exc:
                raise RuntimeError(str(exc)) from exc
        # Escrow is charged the full amount; recipient gets amount − fee.
        # Wallet debit is send + fee == amount_atomic.
        if self.wallet.enabled:
            try:
                unlocked = self.wallet.get_unlocked_atomic()
            except WalletError as exc:
                raise RuntimeError(str(exc)) from exc
            if unlocked < amount_atomic:
                raise ValueError(
                    "hot wallet unlocked balance is "
                    f"{format_amount(unlocked)} PDC; need "
                    f"{format_amount(amount_atomic)} PDC "
                    f"(payout {format_amount(send_atomic)} + fee "
                    f"{format_amount(fee_atomic)}). Escrow remaining can "
                    "exceed the wallet when older fees were not charged "
                    "to the proposal — top up the wallet or pay a smaller amount"
                )
        payout_id = self.db.reserve_payout(
            proposal_id=proposal.id,
            amount_atomic=amount_atomic,
            to_address=dest,
            milestone_note=note,
            created_by=actor.id,
        )
        try:
            tx_hash = self.wallet.transfer(
                dest,
                send_atomic,
                comment=f"pdc-funding proposal #{proposal.id}: {note}"[:120],
            )
        except WalletRejected as exc:
            self._release_reservation(payout_id)
            raise RuntimeError(str(exc)) from exc
        except ValueError:
            self._release_reservation(payout_id)
            raise
        except WalletError as exc:
            logger.error(
                "payout reservation %s kept after uncertain wallet result: %s",
                payout_id,
                exc,
            )
            raise RuntimeError(
                f"payout may have been broadcast (reservation {payout_id}); "
                "escrow stays reserved until an operator reconciles"
            ) from exc
        try:
            payout = self.db.finalize_payout(payout_id, tx_hash)
        except Exception as exc:
            logger.error(
                "payout reservation %s broadcast as %s but was not finalized",
                payout_id,
                tx_hash,
            )
            raise RuntimeError(
                f"payout broadcast as {tx_hash} but reservation {payout_id} "
                "was not finalized"
            ) from exc
        return {
            "payout_id": payout.id,
            "tx_hash": payout.tx_hash,
            "amount_atomic": payout.amount_atomic,
            "send_atomic": send_atomic,
            "fee_atomic": fee_atomic,
            "to_address": payout.to_address,
        }

    def _release_reservation(self, payout_id: int) -> None:
        try:
            self.db.release_payout(payout_id)
        except Exception:
            logger.exception("failed to release payout reservation %s", payout_id)

    def add_comment(self, user: User, proposal_id: int, message: str) -> Comment:
        if not message.strip():
            raise ValueError("empty comment")
        if len(message) > 4000:
            raise ValueError("comment too long")
        proposal = self.db.get_proposal(proposal_id)
        if not proposal:
            raise KeyError("proposal not found")
        return self.db.add_comment(
            proposal_id=proposal_id, user_id=user.id, message=message
        )
