"""PDC/Zano-compatible wallet JSON-RPC client (tipbot pattern)."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from funding.config import Settings

logger = logging.getLogger("pdc-funding.wallet")


class WalletError(RuntimeError):
    """Wallet RPC outcome is unknown, or the call never reached transfer."""


class WalletRejected(WalletError):
    """Node or wallet rejected the call. A transfer was not broadcast."""


# Zano / CryptoNote: unlock_time below this is a block height; otherwise a unix time.
CRYPTONOTE_MAX_BLOCK_NUMBER = 500_000_000


def payment_is_unlocked(unlock_time: int, height: int, *, now: Optional[int] = None) -> bool:
    """Match Zano spend-time rules. height is chain size from daemon getinfo."""
    if unlock_time <= 0:
        return True
    if unlock_time < CRYPTONOTE_MAX_BLOCK_NUMBER:
        if height <= 0:
            return False
        return height >= unlock_time
    current = int(time.time() if now is None else now)
    return current >= unlock_time


def parse_chain_height(data: Any) -> int:
    """Read daemon getinfo height. Accepts a bare object or a JSON-RPC envelope."""
    if not isinstance(data, dict):
        return 0
    body = data.get("result") if isinstance(data.get("result"), dict) else data
    if not isinstance(body, dict):
        return 0
    try:
        height = int(body.get("height") or 0)
    except (TypeError, ValueError):
        return 0
    return height if height > 0 else 0


@dataclass
class Payment:
    payment_id: str
    tx_hash: str
    amount: int
    block_height: int
    unlock_time: int = 0
    asset_id: str = ""


class WalletClient:
    """Talks to simplewallet RPC. Dry-run when URL is empty."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session = requests.Session()
        self._dry_address = (
            "pdcDRYRUNADDRESSPLACEHOLDER000000000000000000000000000000000000000000000000000000000"
        )

    @property
    def enabled(self) -> bool:
        return self.settings.wallet_enabled

    def _call(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        if not self.enabled:
            raise WalletError("wallet RPC disabled (dry-run)")
        payload = {"jsonrpc": "2.0", "id": 0, "method": method, "params": params or {}}
        auth = None
        if self.settings.wallet_rpc_user:
            auth = (self.settings.wallet_rpc_user, self.settings.wallet_rpc_password)
        try:
            resp = self._session.post(
                self.settings.wallet_rpc_url,
                json=payload,
                auth=auth,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise WalletError(f"wallet RPC transport error: {exc}") from exc
        if not isinstance(data, dict):
            raise WalletError("wallet RPC returned a non-object body")
        if data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else err
            code = err.get("code") if isinstance(err, dict) else ""
            raise WalletRejected(f"{code}: {message}")
        return data.get("result")

    def get_address(self) -> str:
        if not self.enabled:
            return self._dry_address
        result = self._call("getaddress")
        return str(result["address"])

    def make_integrated_address(self, payment_id: Optional[str] = None) -> tuple[str, str]:
        pid = (payment_id or secrets.token_hex(8)).lower()
        if len(pid) != 16:
            raise ValueError("payment_id must be 8 bytes hex (16 chars)")
        if not self.enabled:
            return f"i{self._dry_address}{pid}", pid
        result = self._call("make_integrated_address", {"payment_id": pid})
        return str(result["integrated_address"]), str(result["payment_id"]).lower()

    def resolve_destination(self, address_or_alias: str) -> str:
        raw = (address_or_alias or "").strip()
        if not raw:
            raise ValueError("empty destination")
        if not raw.startswith("@"):
            return raw
        alias = raw[1:].strip().lower()
        if not alias:
            raise ValueError("empty alias")
        url = f"{self.settings.daemon_rpc_url.rstrip('/')}/json_rpc"
        payload = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "get_alias_details",
            "params": {"alias": alias},
        }
        try:
            resp = self._session.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise WalletError(f"alias resolve failed: {exc}") from exc
        if not isinstance(data, dict) or data.get("error"):
            raise WalletRejected(f"alias @{alias} not found")
        details = (data.get("result") or {}).get("alias_details") or {}
        address = str(details.get("address") or "")
        if not address:
            raise WalletRejected(f"alias @{alias} has no address")
        return address

    def transfer(self, address: str, amount_atomic: int, comment: str = "") -> str:
        if amount_atomic <= 0:
            raise ValueError("transfer amount must be positive")
        if not self.enabled:
            return f"dryrun_{secrets.token_hex(16)}"
        dest = self.resolve_destination(address)
        params: dict[str, Any] = {
            "destinations": [
                {
                    "address": dest,
                    "amount": amount_atomic,
                    "asset_id": self.settings.pdc_asset_id,
                }
            ],
            "fee": self.settings.transfer_fee_atomic,
            "mixin": 15,
            "push_payer": False,
            "hide_receiver": True,
        }
        if comment:
            params["comment"] = comment
        result = self._call("transfer", params)
        tx_hash = ""
        if isinstance(result, dict) and result.get("tx_hash"):
            tx_hash = str(result["tx_hash"]).strip()
        if not tx_hash:
            # The node may already have broadcast. Callers must not retry blindly.
            raise WalletError("wallet response missing tx_hash")
        return tx_hash

    def get_bulk_payments(
        self,
        payment_ids: list[str],
        min_block_height: int = 0,
        *,
        allow_locked: bool = False,
    ) -> list[Payment]:
        if not payment_ids:
            return []
        if not self.enabled:
            return []
        result = self._call(
            "get_bulk_payments",
            {
                "payment_ids": [p.lower() for p in payment_ids],
                "min_block_height": min_block_height,
                "allow_locked_transactions": bool(allow_locked),
            },
        )
        if not isinstance(result, dict):
            return []
        raw_payments = result.get("payments") or []
        if not isinstance(raw_payments, list):
            return []
        best: dict[tuple[str, str], Payment] = {}
        for item in raw_payments:
            if not isinstance(item, dict):
                continue
            try:
                pid = str(item["payment_id"]).strip().lower()
                tx_hash = str(item["tx_hash"]).strip()
                amount = int(item.get("amount", 0))
                block_height = int(item.get("block_height", 0) or 0)
                unlock_time = int(item.get("unlock_time", 0) or 0)
            except (KeyError, TypeError, ValueError):
                logger.warning("skipping malformed payment record")
                continue
            if not pid or not tx_hash or amount <= 0:
                continue
            raw_asset = item.get("asset_id")
            asset_id = str(raw_asset).strip().lower() if raw_asset else ""
            pay = Payment(
                payment_id=pid,
                tx_hash=tx_hash,
                amount=amount,
                block_height=block_height,
                unlock_time=unlock_time,
                asset_id=asset_id,
            )
            key = (pay.tx_hash.lower(), pay.payment_id)
            prev = best.get(key)
            if prev is None or pay.block_height > prev.block_height:
                best[key] = pay
            elif prev is not None and pay.block_height == prev.block_height:
                if pay.unlock_time == 0 and prev.unlock_time != 0:
                    best[key] = pay
                elif pay.amount > prev.amount:
                    best[key] = pay
        return list(best.values())

    def get_height(self) -> int:
        if not self.enabled and not self.settings.daemon_rpc_url:
            return 0
        url = f"{self.settings.daemon_rpc_url.rstrip('/')}/getinfo"
        try:
            resp = self._session.get(url, timeout=20)
            resp.raise_for_status()
            return parse_chain_height(resp.json())
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning("daemon getinfo height failed: %s", exc)
            return 0
