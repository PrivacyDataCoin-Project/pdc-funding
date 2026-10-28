"""Credit on-chain donations to proposals via payment_id."""

from __future__ import annotations

import asyncio
import logging

from funding.config import Settings
from funding.db import Database
from funding.wallet import WalletClient, WalletError

logger = logging.getLogger("pdc-funding.scanner")

META_WATERMARK = "deposit_scan_min_height"


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

    def scan_once(self) -> int:
        if not self.wallet.enabled:
            return 0
        proposals = self.db.proposals_needing_scan()
        if not proposals:
            return 0
        payment_ids = [p.payment_id for p in proposals if p.payment_id]
        by_pid = {p.payment_id: p for p in proposals if p.payment_id}
        try:
            height = self.wallet.get_height()
        except WalletError:
            height = 0
        min_height_raw = self.db.get_meta(META_WATERMARK, "0")
        try:
            watermark = max(0, int(min_height_raw) - 5)
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
        for pay in payments:
            proposal = by_pid.get(pay.payment_id.lower())
            if not proposal:
                continue
            if height and pay.block_height:
                confs = height - pay.block_height + 1
                if confs < self.settings.deposit_min_confirmations:
                    continue
            if pay.unlock_time and height and pay.unlock_time > height:
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
            self.db.set_meta(META_WATERMARK, str(max(0, height - 20)))
        return credited
