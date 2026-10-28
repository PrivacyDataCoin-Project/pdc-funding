"""Domain logic for PDC Funding."""

from __future__ import annotations

import logging
import re
from typing import Optional

import bcrypt

from funding.amounts import funding_pct, parse_amount
from funding.config import CATEGORIES, STATUSES, Settings
from funding.db import Comment, Database, Proposal, User
from funding.wallet import WalletClient, WalletError

logger = logging.getLogger("pdc-funding.service")

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
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
        is_admin = force_admin or self.db.user_count() == 0
        return self.db.create_user(
            username,
            email,
            self.hash_password(password),
            is_admin=is_admin,
        )

    def authenticate(self, username: str, password: str) -> Optional[User]:
        user = self.db.get_user_by_username(username)
        if not user:
            return None
        if not self.verify_password(password, user.password_hash):
            return None
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
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(CATEGORIES)}")
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
        self.db.set_donation_address(proposal.id, pid, address)
        refreshed = self.db.get_proposal(proposal.id)
        if not refreshed:
            raise RuntimeError("proposal vanished after address assign")
        return refreshed

    def set_status(self, actor: User, proposal_id: int, status: int) -> Proposal:
        if not actor.is_admin:
            raise PermissionError("admin required")
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
        available = max(0, received - paid)
        return {
            "received_atomic": received,
            "paid_atomic": paid,
            "available_atomic": available,
            "target_atomic": int(proposal.funds_target_atomic),
            "pct": funding_pct(received, int(proposal.funds_target_atomic)),
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
        if not actor.is_admin:
            raise PermissionError("admin required")
        proposal = self.db.get_proposal(proposal_id)
        if not proposal:
            raise KeyError("proposal not found")
        if proposal.status not in (2, 3, 4):
            raise ValueError("proposal must be funding/wip/completed to pay out")
        amount_atomic = parse_amount(amount)
        available = self.available_atomic(proposal)
        if amount_atomic > available:
            raise ValueError("amount exceeds available escrow balance")
        dest = (to_address or proposal.payout_address or "").strip()
        if not dest:
            raise ValueError("payout address required")
        try:
            tx_hash = self.wallet.transfer(
                dest,
                amount_atomic,
                comment=f"pdc-funding proposal #{proposal.id}: {milestone_note}"[:120],
            )
        except WalletError as exc:
            raise RuntimeError(str(exc)) from exc
        payout = self.db.add_payout(
            proposal_id=proposal.id,
            amount_atomic=amount_atomic,
            to_address=dest,
            tx_hash=tx_hash,
            milestone_note=milestone_note.strip(),
            created_by=actor.id,
        )
        return {
            "payout_id": payout.id,
            "tx_hash": payout.tx_hash,
            "amount_atomic": payout.amount_atomic,
            "to_address": payout.to_address,
        }

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
