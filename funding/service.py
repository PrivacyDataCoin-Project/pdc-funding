"""Domain logic for PDC Funding."""

from __future__ import annotations

import logging
import re
import secrets
from typing import Optional

import bcrypt

from funding.amounts import format_amount, funding_pct, parse_amount
from funding.assets import (
    ASSET_BTC,
    ASSET_BTCX,
    ASSET_PDC,
    ASSETS,
    asset_id_for,
    format_amount as format_amount_asset,
    normalize_asset,
    parse_amount as parse_amount_asset,
    parse_assets_csv,
)
from funding.btc_wallet import BtcWalletClient, BtcWalletError, BtcWalletRejected
from funding.config import CATEGORIES, STATUSES, Settings
from funding.db import Comment, Database, Proposal, User
from funding.fee_convert import FeeCoverPlan, plan_pdc_fee_cover
from funding.prices import fetch_prices
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
    def __init__(
        self,
        settings: Settings,
        db: Database,
        wallet: WalletClient,
        btc_wallet: BtcWalletClient | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.wallet = wallet
        self.btc_wallet = btc_wallet or BtcWalletClient(settings)
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
        funds_target: str = "",
        payout_address: str = "",
        assets: Optional[list[str]] = None,
        targets: Optional[dict[str, str]] = None,
        payout_addresses: Optional[dict[str, str]] = None,
    ) -> Proposal:
        if not headline.strip() or len(headline.strip()) > 200:
            raise ValueError("headline required (max 200 chars)")
        if not content.strip():
            raise ValueError("content required")
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError("content too long")
        if category not in CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(CATEGORIES)}")
        selected = parse_assets_csv(assets) if assets is not None else []
        targets = dict(targets or {})
        payout_addresses = dict(payout_addresses or {})
        # Backward compatible: single funds_target ⇒ PDC only.
        if not selected:
            if not (funds_target or "").strip():
                raise ValueError("select at least one asset and target")
            selected = [ASSET_PDC]
            targets = {ASSET_PDC: funds_target}
            if payout_address:
                payout_addresses = {ASSET_PDC: payout_address}
        if not selected:
            raise ValueError("select at least one asset: PDC, BTCX, and/or BTC")
        # Free choice: any one asset, or any combination. L1 fee for BTCX payouts
        # is covered at payout time from PDC escrow and/or oracle conversion of
        # leftover BTCX/BTC — never force a PDC lane at create time.
        parsed: list[tuple[str, int, str]] = []
        for ticker in selected:
            raw_amt = (targets.get(ticker) or "").strip()
            if not raw_amt:
                raise ValueError(f"target amount required for {ticker}")
            atomic = parse_amount_asset(raw_amt, ticker)
            addr = (payout_addresses.get(ticker) or payout_address or "").strip()
            if len(addr) > _MAX_ADDRESS_CHARS:
                raise ValueError(f"payout address too long for {ticker}")
            parsed.append((ticker, atomic, addr))
        # Legacy header columns keep the first / PDC target for list UI.
        primary = next((p for p in parsed if p[0] == ASSET_PDC), parsed[0])
        proposal = self.db.create_proposal(
            user_id=user.id,
            headline=headline,
            content=content,
            category=category,
            funds_target_atomic=primary[1],
            payout_address=primary[2],
            status=1,
        )
        for ticker, atomic, addr in parsed:
            existing = self.db.get_proposal_asset(proposal.id, ticker)
            if existing:
                continue
            self.db.add_proposal_asset(
                proposal_id=proposal.id,
                asset=ticker,
                funds_target_atomic=atomic,
                payout_address=addr,
            )
        refreshed = self.db.get_proposal(proposal.id)
        assert refreshed is not None
        return refreshed

    def ensure_donation_address(self, proposal: Proposal) -> Proposal:
        """Assign deposit addresses for each selected asset when funding opens."""
        if proposal.status < 2:
            return proposal
        assets = self.db.list_proposal_assets(proposal.id)
        if not assets:
            # Legacy proposal without rows — treat as PDC.
            self.db.add_proposal_asset(
                proposal_id=proposal.id,
                asset=ASSET_PDC,
                funds_target_atomic=int(proposal.funds_target_atomic),
                payout_address=proposal.payout_address,
            )
            assets = self.db.list_proposal_assets(proposal.id)

        # One shared L1 integrated address for all PDC-chain assets on this proposal.
        l1_assets = [a for a in assets if ASSETS[a.asset].chain == "pdc"]
        shared_pid = ""
        shared_addr = ""
        for a in l1_assets:
            if a.payment_id and a.integrated_address:
                shared_pid = a.payment_id
                shared_addr = a.integrated_address
                break
        if not shared_pid and proposal.payment_id and proposal.integrated_address:
            shared_pid = proposal.payment_id
            shared_addr = proposal.integrated_address
        if l1_assets and not shared_pid:
            shared_addr, shared_pid = self.wallet.make_integrated_address()
        for a in l1_assets:
            if a.payment_id and a.integrated_address:
                continue
            self.db.set_asset_l1_address_if_empty(
                proposal.id, a.asset, shared_pid, shared_addr
            )

        for a in assets:
            if a.asset != ASSET_BTC:
                continue
            if a.btc_address:
                continue
            addr = self.btc_wallet.getnewaddress(
                label=f"funding-proposal-{proposal.id}"
            )
            self.db.set_asset_btc_address_if_empty(proposal.id, addr)

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

    def available_atomic(self, proposal: Proposal, asset: str = ASSET_PDC) -> int:
        row = self.db.get_proposal_asset(proposal.id, asset)
        if not row:
            # Legacy proposals have no proposal_assets rows — treat header as PDC.
            # Multi-asset proposals without a PDC lane must not treat other-asset
            # deposits as PDC available (proposal.received_atomic is unscoped).
            if asset == ASSET_PDC and not self.db.list_proposal_assets(proposal.id):
                return max(0, int(proposal.received_atomic) - int(proposal.paid_atomic))
            return 0
        return max(0, int(row.received_atomic) - int(row.paid_atomic))

    def progress(self, proposal: Proposal) -> dict:
        assets = self.db.list_proposal_assets(proposal.id)
        per_asset = []
        for row in assets:
            available = max(0, int(row.received_atomic) - int(row.paid_atomic))
            per_asset.append(
                {
                    "asset": row.asset,
                    "received_atomic": int(row.received_atomic),
                    "paid_atomic": int(row.paid_atomic),
                    "pending_atomic": int(row.pending_atomic),
                    "available_atomic": available,
                    "target_atomic": int(row.funds_target_atomic),
                    "pct": funding_pct(
                        int(row.received_atomic), int(row.funds_target_atomic)
                    ),
                    "paid_pct": funding_pct(
                        int(row.paid_atomic), int(row.received_atomic)
                    )
                    if row.received_atomic > 0
                    else 0.0,
                    "payment_id": row.payment_id,
                    "integrated_address": row.integrated_address,
                    "btc_address": row.btc_address,
                    "payout_address": row.payout_address,
                }
            )
        # Legacy aggregate (PDC-first) for templates that still use top-level fields.
        pdc = next((a for a in per_asset if a["asset"] == ASSET_PDC), None)
        primary = pdc or (per_asset[0] if per_asset else None)
        if primary is None:
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
                "assets": [],
            }
        return {
            **primary,
            "assets": per_asset,
        }

    def _fee_for_asset(self, asset: str) -> int:
        """Escrow fee deducted in the same asset as the payout.

        PDC: L1 fee from PDC escrow. BTCX: 0 on BTCX ledger (L1 fee reserved
        separately from this proposal's PDC escrow). BTC: 0 (miner fee taken
        from the sent amount via subtractfeefromamount).
        """
        ticker = normalize_asset(asset)
        if ticker == ASSET_PDC:
            return int(self.settings.transfer_fee_atomic)
        return 0

    def payout_milestone(
        self,
        actor: User,
        proposal_id: int,
        *,
        amount: str,
        to_address: Optional[str] = None,
        milestone_note: str = "",
        asset: str = ASSET_PDC,
    ) -> dict:
        """Pay a milestone from this proposal's funded escrow only (hard fee model)."""
        self.require_primary_admin(actor)
        ticker = normalize_asset(asset)
        proposal = self.db.get_proposal(proposal_id)
        if not proposal:
            raise KeyError("proposal not found")
        if proposal.status not in (2, 3, 4):
            raise ValueError("proposal must be funding/wip/completed to pay out")
        asset_row = self.db.get_proposal_asset(proposal.id, ticker)
        if not asset_row:
            raise ValueError(f"proposal has no {ticker} target")
        note = (milestone_note or "").strip()
        if len(note) > _MAX_NOTE_CHARS:
            raise ValueError("note too long")
        amount_atomic = parse_amount_asset(amount, ticker)
        l1_fee = int(self.settings.transfer_fee_atomic)
        fee_atomic = self._fee_for_asset(ticker)
        fee_plan: Optional[FeeCoverPlan] = None
        # Hard model: every network cost is covered by this proposal's escrow.
        # PDC: amount includes fee → recipient gets amount − fee.
        # BTCX: full BTCX to recipient; L1 fee from PDC escrow, else oracle-convert
        #        from leftover BTCX/BTC on the same proposal.
        # BTC: full amount reserved; miner fee subtracted from amount on-chain.
        if ticker == ASSET_PDC:
            if amount_atomic <= fee_atomic:
                raise ValueError(
                    f"amount must be greater than transfer fee "
                    f"({format_amount_asset(fee_atomic, ASSET_PDC)} PDC)"
                )
            send_atomic = amount_atomic - fee_atomic
        elif ticker == ASSET_BTCX:
            send_atomic = amount_atomic
            if l1_fee > 0:
                btcx_avail = self.available_atomic(proposal, ASSET_BTCX)
                if btcx_avail < amount_atomic:
                    raise ValueError("amount exceeds available BTCX escrow balance")
                pdc_avail = self.available_atomic(proposal, ASSET_PDC)
                btcx_left = btcx_avail - amount_atomic
                btc_left = self.available_atomic(proposal, ASSET_BTC)
                book = fetch_prices(self.settings)
                fee_plan = plan_pdc_fee_cover(
                    l1_fee,
                    available_pdc=pdc_avail,
                    available_btcx=btcx_left,
                    available_btc=btc_left,
                    book=book,
                )
        else:  # BTC
            min_btc = max(int(self.settings.btc_transfer_fee_sats), 546)
            if amount_atomic <= min_btc:
                raise ValueError(
                    "BTC amount must exceed dust/miner floor "
                    f"({format_amount_asset(min_btc, ASSET_BTC)} BTC)"
                )
            send_atomic = amount_atomic
            fee_atomic = 0
        if send_atomic <= 0:
            raise ValueError("send amount must be positive after fee")
        dest = (to_address or asset_row.payout_address or proposal.payout_address or "").strip()
        if not dest:
            raise ValueError("payout address required")
        if len(dest) > _MAX_ADDRESS_CHARS:
            raise ValueError("payout address too long")

        if ticker in (ASSET_PDC, ASSET_BTCX):
            if self.wallet.enabled:
                try:
                    dest = self.wallet.resolve_destination(dest)
                except WalletError as exc:
                    raise RuntimeError(str(exc)) from exc
                try:
                    unlocked = self.wallet.get_unlocked_atomic(
                        asset_id=asset_id_for(self.settings, ticker)
                    )
                except WalletError as exc:
                    raise RuntimeError(str(exc)) from exc
                need = amount_atomic if ticker == ASSET_PDC else send_atomic
                if fee_plan and fee_plan.btcx_from_escrow:
                    need += int(fee_plan.btcx_from_escrow)
                if unlocked < need:
                    raise ValueError(
                        f"hot wallet unlocked {ticker} is "
                        f"{format_amount_asset(unlocked, ticker)}; need "
                        f"{format_amount_asset(need, ticker)}"
                    )
                if ticker == ASSET_BTCX and l1_fee > 0:
                    try:
                        pdc_unlocked = self.wallet.get_unlocked_atomic(
                            asset_id=asset_id_for(self.settings, ASSET_PDC)
                        )
                    except WalletError as exc:
                        raise RuntimeError(str(exc)) from exc
                    if pdc_unlocked < l1_fee:
                        raise ValueError(
                            "hot wallet unlocked PDC is "
                            f"{format_amount_asset(pdc_unlocked, ASSET_PDC)}; "
                            "need "
                            f"{format_amount_asset(l1_fee, ASSET_PDC)} PDC "
                            "liquid for L1 fee. Escrow may cover via BTCX/BTC "
                            "conversion, but the funding L1 wallet still needs a "
                            "PDC fee buffer (top up once; later BTCX converts "
                            "reimburse the pool)"
                        )
        elif ticker == ASSET_BTC and self.btc_wallet.enabled:
            try:
                unlocked = self.btc_wallet.get_balance_sats()
            except BtcWalletError as exc:
                raise RuntimeError(str(exc)) from exc
            need_btc = amount_atomic
            if fee_plan and fee_plan.btc_from_escrow:
                need_btc += int(fee_plan.btc_from_escrow)
            if unlocked < need_btc:
                raise ValueError(
                    f"BTC wallet unlocked is {format_amount_asset(unlocked, ASSET_BTC)}; "
                    f"need {format_amount_asset(need_btc, ASSET_BTC)}"
                )

        reserved_ids: list[int] = []
        try:
            payout_id = self.db.reserve_payout(
                proposal_id=proposal.id,
                amount_atomic=amount_atomic,
                to_address=dest,
                milestone_note=note,
                created_by=actor.id,
                asset=ticker,
            )
            reserved_ids.append(payout_id)
            if ticker == ASSET_BTCX and fee_plan and l1_fee > 0:
                if fee_plan.pdc_from_escrow > 0:
                    reserved_ids.append(
                        self.db.reserve_payout(
                            proposal_id=proposal.id,
                            amount_atomic=fee_plan.pdc_from_escrow,
                            to_address="l1-network-fee",
                            milestone_note=(
                                f"L1 fee PDC for BTCX payout ({note})"
                            )[:_MAX_NOTE_CHARS],
                            created_by=actor.id,
                            asset=ASSET_PDC,
                        )
                    )
                if fee_plan.btcx_from_escrow > 0:
                    reserved_ids.append(
                        self.db.reserve_payout(
                            proposal_id=proposal.id,
                            amount_atomic=fee_plan.btcx_from_escrow,
                            to_address="l1-fee-convert",
                            milestone_note=(
                                f"L1 fee convert BTCX→PDC ({note})"
                            )[:_MAX_NOTE_CHARS],
                            created_by=actor.id,
                            asset=ASSET_BTCX,
                        )
                    )
                if fee_plan.btc_from_escrow > 0:
                    reserved_ids.append(
                        self.db.reserve_payout(
                            proposal_id=proposal.id,
                            amount_atomic=fee_plan.btc_from_escrow,
                            to_address="l1-fee-convert",
                            milestone_note=(
                                f"L1 fee convert BTC→PDC ({note})"
                            )[:_MAX_NOTE_CHARS],
                            created_by=actor.id,
                            asset=ASSET_BTC,
                        )
                    )
        except Exception:
            for rid in reserved_ids:
                self._release_reservation(rid)
            raise

        try:
            if ticker == ASSET_BTC:
                tx_hash = self.btc_wallet.send_to_address(
                    dest,
                    send_atomic,
                    comment=f"pdc-funding #{proposal.id}: {note}"[:100],
                    subtract_fee_from_amount=True,
                )
            else:
                tx_hash = self.wallet.transfer(
                    dest,
                    send_atomic,
                    comment=f"pdc-funding proposal #{proposal.id}: {note}"[:120],
                    asset_id=asset_id_for(self.settings, ticker),
                    fee_atomic=l1_fee if l1_fee > 0 else 0,
                )
        except (WalletRejected, BtcWalletRejected) as exc:
            for rid in reserved_ids:
                self._release_reservation(rid)
            raise RuntimeError(str(exc)) from exc
        except ValueError:
            for rid in reserved_ids:
                self._release_reservation(rid)
            raise
        except (WalletError, BtcWalletError) as exc:
            logger.error(
                "payout reservations %s kept after uncertain wallet result: %s",
                reserved_ids,
                exc,
            )
            raise RuntimeError(
                f"payout may have been broadcast (reservations {reserved_ids}); "
                "escrow stays reserved until an operator reconciles"
            ) from exc
        try:
            payout = self.db.finalize_payout(reserved_ids[0], tx_hash)
            for rid in reserved_ids[1:]:
                self.db.finalize_payout(rid, tx_hash)
        except Exception as exc:
            logger.error(
                "payout reservations %s broadcast as %s but not finalized",
                reserved_ids,
                tx_hash,
            )
            raise RuntimeError(
                f"payout broadcast as {tx_hash} but reservations {reserved_ids} "
                "were not finalized"
            ) from exc
        return {
            "payout_id": payout.id,
            "tx_hash": payout.tx_hash,
            "amount_atomic": payout.amount_atomic,
            "send_atomic": send_atomic,
            "fee_atomic": l1_fee if ticker == ASSET_BTCX else fee_atomic,
            "fee_asset": ASSET_PDC if ticker in (ASSET_PDC, ASSET_BTCX) else ASSET_BTC,
            "fee_cover": (
                {
                    "pdc": fee_plan.pdc_from_escrow,
                    "btcx": fee_plan.btcx_from_escrow,
                    "btc": fee_plan.btc_from_escrow,
                }
                if fee_plan
                else None
            ),
            "asset": ticker,
            "to_address": payout.to_address,
        }

    def _release_reservation(self, payout_id: int) -> None:
        try:
            self.db.release_payout(payout_id)
        except Exception:
            logger.exception("failed to release payout reservation %s", payout_id)

    def treasury_stats(self) -> dict:
        """Hot-wallet balances + per-proposal escrow ledger for admin panel."""
        hot: dict[str, dict] = {}
        for ticker in (ASSET_PDC, ASSET_BTCX):
            entry: dict = {
                "asset": ticker,
                "unlocked_atomic": 0,
                "mode": "dry-run",
                "error": None,
            }
            if self.wallet.enabled:
                entry["mode"] = "live"
                try:
                    entry["unlocked_atomic"] = int(
                        self.wallet.get_unlocked_atomic(
                            asset_id=asset_id_for(self.settings, ticker)
                        )
                    )
                except WalletError as exc:
                    entry["error"] = str(exc)
            hot[ticker] = entry

        btc_entry: dict = {
            "asset": ASSET_BTC,
            "unlocked_atomic": 0,
            "mode": "dry-run",
            "error": None,
            "wallet_name": self.btc_wallet.wallet_name,
        }
        if self.btc_wallet.enabled:
            btc_entry["mode"] = "live"
            try:
                btc_entry["unlocked_atomic"] = int(self.btc_wallet.get_balance_sats())
            except BtcWalletError as exc:
                btc_entry["error"] = str(exc)
        hot[ASSET_BTC] = btc_entry

        proposals = self.db.list_proposals(limit=500, offset=0)
        per_proposal: list[dict] = []
        escrow_totals = {
            ASSET_PDC: {"received": 0, "paid": 0, "pending": 0, "available": 0},
            ASSET_BTCX: {"received": 0, "paid": 0, "pending": 0, "available": 0},
            ASSET_BTC: {"received": 0, "paid": 0, "pending": 0, "available": 0},
        }
        for prop in proposals:
            assets = self.db.list_proposal_assets(prop.id)
            rows = []
            for row in assets:
                received = int(row.received_atomic)
                paid = int(row.paid_atomic)
                pending = int(row.pending_atomic)
                available = max(0, received - paid)
                ticker = row.asset
                if ticker in escrow_totals:
                    escrow_totals[ticker]["received"] += received
                    escrow_totals[ticker]["paid"] += paid
                    escrow_totals[ticker]["pending"] += pending
                    escrow_totals[ticker]["available"] += available
                rows.append(
                    {
                        "asset": ticker,
                        "received_atomic": received,
                        "paid_atomic": paid,
                        "pending_atomic": pending,
                        "available_atomic": available,
                        "target_atomic": int(row.funds_target_atomic),
                        "payment_id": row.payment_id,
                        "btc_address": row.btc_address,
                        "integrated_address": row.integrated_address,
                    }
                )
            if not rows and (prop.received_atomic or prop.paid_atomic):
                # Legacy header-only view
                received = int(prop.received_atomic)
                paid = int(prop.paid_atomic)
                pending = int(getattr(prop, "pending_atomic", 0) or 0)
                available = max(0, received - paid)
                escrow_totals[ASSET_PDC]["received"] += received
                escrow_totals[ASSET_PDC]["paid"] += paid
                escrow_totals[ASSET_PDC]["pending"] += pending
                escrow_totals[ASSET_PDC]["available"] += available
                rows.append(
                    {
                        "asset": ASSET_PDC,
                        "received_atomic": received,
                        "paid_atomic": paid,
                        "pending_atomic": pending,
                        "available_atomic": available,
                        "target_atomic": int(prop.funds_target_atomic),
                        "payment_id": prop.payment_id,
                        "btc_address": "",
                        "integrated_address": prop.integrated_address,
                    }
                )
            per_proposal.append(
                {
                    "id": prop.id,
                    "headline": prop.headline,
                    "status": prop.status,
                    "status_label": STATUSES.get(prop.status, str(prop.status)),
                    "author": prop.author,
                    "assets": rows,
                }
            )

        unallocated = {}
        for ticker, tot in escrow_totals.items():
            hot_unlocked = int(hot[ticker]["unlocked_atomic"])
            # Coins in hot wallet beyond open escrow available (fees buffer / dust / other).
            unallocated[ticker] = hot_unlocked - int(tot["available"])

        return {
            "hot": hot,
            "escrow_totals": escrow_totals,
            "unallocated": unallocated,
            "proposals": per_proposal,
            "l1_wallet_enabled": self.wallet.enabled,
            "btc_wallet_enabled": self.btc_wallet.enabled,
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
