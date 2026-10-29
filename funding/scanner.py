"""Credit on-chain donations to proposals via payment_id."""

from __future__ import annotations

import asyncio
import logging

from funding.config import Settings
from funding.db import Database
from funding.wallet import Payment, WalletClient, WalletError, payment_is_unlocked

logger = logging.getLogger("pdc-funding.scanner")

META_WATERMARK = "deposit_scan_min_height"
WATERMARK_MIGRATION = "deposit_scan_watermark_v2"
# Extra blocks kept in the query window so a short reorg can still be credited.
REORG_MARGIN_BLOCKS = 5


class DepositScanner:
    def __init__(
        self, settings: Settings, db: Database, wallet: WalletClient
    ) -> None:
        self.settings = settings
        self.db = db
        self.wallet = wallet
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="deposit-scanner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                credited = await asyncio.to_thread(self.scan_once)
                if credited:
                    logger.info("credited %d donation(s)", credited)
            except Exception:
                logger.exception("deposit scan failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.deposit_scan_interval_sec,
                )
            except asyncio.TimeoutError:
                pass

    def _migrate_watermark(self) -> None:
        """One-time rewind. Older builds advanced the cursor past unconfirmed txs."""
        if self.db.get_meta(WATERMARK_MIGRATION) == "1":
            return
        self.db.set_meta(META_WATERMARK, "0")
        self.db.set_meta(WATERMARK_MIGRATION, "1")
        logger.info("deposit scan watermark reset once to avoid skipping payments")

    def _payment_ready(self, pay: Payment, height: int) -> tuple[bool, bool]:
        """Return (credit_now, retry_later).

        Daemon ``height`` is chain size (Zano getinfo). Wallet ``block_height`` is
        the block index. Confirmations are ``height - block_height`` (no +1).
        """
        if pay.amount <= 0:
            return False, False
        min_conf = self.settings.deposit_min_confirmations
        if min_conf > 0:
            if height <= 0 or pay.block_height <= 0:
                return False, True
            confirmations = height - pay.block_height
            if confirmations < min_conf:
                return False, True
        if not payment_is_unlocked(pay.unlock_time, height):
            return False, True
        return True, False

    def scan_once(self) -> int:
        if not self.wallet.enabled:
            return 0
        self._migrate_watermark()
        proposals = self.db.proposals_needing_scan()
        if not proposals:
            return 0
        by_pid = {}
        payment_ids: list[str] = []
        for proposal in proposals:
            if not proposal.payment_id:
                continue
            pid = proposal.payment_id.lower()
            by_pid[pid] = proposal
            payment_ids.append(pid)
        if not payment_ids:
            return 0
        min_conf = self.settings.deposit_min_confirmations
        try:
            height = self.wallet.get_height()
        except WalletError:
            height = 0
        if min_conf > 0 and height <= 0:
            logger.warning("deposit scan skipped: chain height unknown")
            return 0
        min_height_raw = self.db.get_meta(META_WATERMARK, "0")
        try:
            watermark = max(0, int(min_height_raw))
        except ValueError:
            watermark = 0
        try:
            payments = self.wallet.get_bulk_payments(
                payment_ids, min_block_height=watermark, allow_locked=True
            )
        except WalletError as exc:
            logger.warning("get_bulk_payments: %s", exc)
            return 0

        credited = 0
        hold_at: list[int] = []
        expected_asset = self.settings.pdc_asset_id.strip().lower()
        for pay in payments:
            proposal = by_pid.get(pay.payment_id.lower())
            if not proposal or not proposal.payment_id:
                continue
            if pay.payment_id.lower() != proposal.payment_id.lower():
                continue
            if pay.asset_id and expected_asset and pay.asset_id.lower() != expected_asset:
                logger.warning(
                    "skipping non-PDC payment tx=%s asset=%s",
                    pay.tx_hash,
                    pay.asset_id,
                )
                continue
            ready, retry = self._payment_ready(pay, height)
            if not ready:
                if retry and pay.block_height > 0:
                    hold_at.append(pay.block_height)
                continue
            if self.db.credit_deposit(
                proposal_id=proposal.id,
                payment_id=pay.payment_id,
                tx_hash=pay.tx_hash,
                amount_atomic=pay.amount,
                block_height=pay.block_height,
            ):
                credited += 1
                logger.info(
                    "credited proposal #%s %s atomic tx=%s",
                    proposal.id,
                    pay.amount,
                    pay.tx_hash,
                )
        if height > 0:
            mature_floor = max(0, height - min_conf - REORG_MARGIN_BLOCKS)
            next_mark = mature_floor
            if hold_at:
                # Stay strictly below the oldest block we still need, whether
                # the wallet treats min_block_height as inclusive or exclusive.
                next_mark = min(next_mark, max(0, min(hold_at) - 1))
            self.db.set_meta(META_WATERMARK, str(max(0, next_mark)))
        return credited
