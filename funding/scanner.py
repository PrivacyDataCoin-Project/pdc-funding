"""Credit on-chain donations to proposals (PDC/BTCX on L1, BTC via bitcoind)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from funding.assets import ASSET_BTC, ASSET_BTCX, ASSET_PDC, resolve_payment_ticker
from funding.btc_wallet import BtcWalletClient, BtcWalletError
from funding.config import Settings
from funding.db import Database
from funding.wallet import Payment, WalletClient, WalletError, payment_is_unlocked

logger = logging.getLogger("pdc-funding.scanner")

META_WATERMARK = "deposit_scan_min_height"
WATERMARK_MIGRATION = "deposit_scan_watermark_v2"
META_BTC_WATERMARK = "btc_deposit_scan_sats_mark"
REORG_MARGIN_BLOCKS = 5


class DepositScanner:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        wallet: WalletClient,
        btc_wallet: Optional[BtcWalletClient] = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.wallet = wallet
        self.btc_wallet = btc_wallet or BtcWalletClient(settings)
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
        if self.db.get_meta(WATERMARK_MIGRATION) == "1":
            return
        self.db.set_meta(META_WATERMARK, "0")
        self.db.set_meta(WATERMARK_MIGRATION, "1")
        logger.info("deposit scan watermark reset once to avoid skipping payments")

    def _payment_ready(self, pay: Payment, height: int) -> tuple[bool, bool]:
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
        credited = 0
        credited += self._scan_l1()
        credited += self._scan_btc()
        return credited

    def _scan_l1(self) -> int:
        if not self.wallet.enabled:
            return 0
        self._migrate_watermark()
        assets = self.db.assets_needing_l1_scan()
        # Also include legacy proposals_needing_scan for PDC-only rows.
        if not assets:
            legacy = self.db.proposals_needing_scan()
            # Synthesize from proposals that still only have payment_id on header.
            for proposal in legacy:
                if not proposal.payment_id:
                    continue
                # Ensure a PDC asset row exists (migration should have done this).
                row = self.db.get_proposal_asset(proposal.id, ASSET_PDC)
                if row and row.payment_id:
                    assets.append(row)
        if not assets:
            return 0
        by_pid: dict[str, list] = {}
        payment_ids: list[str] = []
        for asset_row in assets:
            if not asset_row.payment_id:
                continue
            pid = asset_row.payment_id.lower()
            by_pid.setdefault(pid, []).append(asset_row)
            if pid not in payment_ids:
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
        for pay in payments:
            rows = by_pid.get(pay.payment_id.lower()) or []
            if not rows:
                continue
            # One payment_id must map to a single proposal (PDC+BTCX share within it).
            if len({r.proposal_id for r in rows}) > 1:
                logger.error(
                    "payment_id %s maps to multiple proposals; refusing credit",
                    pay.payment_id,
                )
                continue
            proposal_tickers = {r.asset for r in rows}
            ticker = resolve_payment_ticker(
                self.settings, pay.asset_id, proposal_tickers=proposal_tickers
            )
            if ticker is None:
                logger.warning(
                    "skipping ambiguous/unknown asset payment tx=%s asset=%s "
                    "tickers=%s",
                    pay.tx_hash,
                    pay.asset_id or "(empty)",
                    sorted(proposal_tickers),
                )
                continue
            # Match the proposal_asset row for this ticker (same payment_id shared).
            asset_row = next((r for r in rows if r.asset == ticker), None)
            if asset_row is None:
                # Payment for an asset this proposal did not request.
                continue
            ready, retry = self._payment_ready(pay, height)
            if not ready:
                if retry and pay.block_height > 0:
                    hold_at.append(pay.block_height)
                continue
            if self.db.credit_deposit(
                proposal_id=asset_row.proposal_id,
                payment_id=pay.payment_id,
                tx_hash=pay.tx_hash,
                amount_atomic=pay.amount,
                block_height=pay.block_height,
                asset=ticker,
            ):
                credited += 1
                logger.info(
                    "credited proposal #%s %s %s atomic tx=%s",
                    asset_row.proposal_id,
                    ticker,
                    pay.amount,
                    pay.tx_hash,
                )
        if height > 0:
            mature_floor = max(0, height - min_conf - REORG_MARGIN_BLOCKS)
            next_mark = mature_floor
            if hold_at:
                next_mark = min(next_mark, max(0, min(hold_at) - 1))
            self.db.set_meta(META_WATERMARK, str(max(0, next_mark)))
        return credited

    def _scan_btc(self) -> int:
        """Credit BTC by comparing getreceivedbyaddress to already-credited sum.

        Dry-run / disabled BTC RPC: no-op. Does not broadcast or spend.
        """
        if not self.btc_wallet.enabled:
            return 0
        assets = self.db.assets_needing_btc_scan()
        if not assets:
            return 0
        minconf = int(self.settings.btc_min_confirmations)
        credited = 0
        try:
            info = self.btc_wallet.get_blockchain_info()
            height = int(info.get("blocks") or 0)
        except BtcWalletError as exc:
            logger.warning("BTC scan height: %s", exc)
            height = 0
        for asset_row in assets:
            addr = (asset_row.btc_address or "").strip()
            if not addr:
                continue
            try:
                received_sats = self.btc_wallet.get_received_by_address(
                    addr, minconf=minconf
                )
            except BtcWalletError as exc:
                logger.warning("BTC getreceivedbyaddress %s: %s", addr[:12], exc)
                continue
            # Cumulative credit under DB lock — never double-count if height/timing races.
            delta = self.db.credit_btc_cumulative(
                proposal_id=asset_row.proposal_id,
                btc_address=addr,
                chain_received_sats=received_sats,
                block_height=height,
            )
            if delta > 0:
                credited += 1
                logger.info(
                    "credited proposal #%s BTC +%s sats addr=%s…",
                    asset_row.proposal_id,
                    delta,
                    addr[:12],
                )
        return credited
