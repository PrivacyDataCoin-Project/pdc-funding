"""Bitcoin Core JSON-RPC client for native BTC escrow (separate from bridge wallet)."""

from __future__ import annotations

import logging
import secrets
from typing import Any, Optional

import requests

from funding.config import Settings

logger = logging.getLogger("pdc-funding.btc_wallet")


class BtcWalletError(RuntimeError):
    pass


class BtcWalletRejected(BtcWalletError):
    pass


class BtcWalletClient:
    """Named bitcoind wallet for funding deposits/payouts. Dry-run when URL empty."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.btc_rpc_url)

    @property
    def wallet_name(self) -> str:
        return (self.settings.btc_wallet_name or "funding").strip() or "funding"

    def _endpoint(self) -> str:
        base = self.settings.btc_rpc_url.rstrip("/")
        # Prefer wallet-qualified endpoint: /wallet/<name>
        return f"{base}/wallet/{self.wallet_name}"

    def _call(self, method: str, params: Optional[list[Any]] = None) -> Any:
        if not self.enabled:
            raise BtcWalletError("BTC RPC disabled (dry-run)")
        payload = {
            "jsonrpc": "1.0",
            "id": "pdc-funding",
            "method": method,
            "params": params or [],
        }
        auth = None
        if self.settings.btc_rpc_user:
            auth = (self.settings.btc_rpc_user, self.settings.btc_rpc_password)
        try:
            resp = self._session.post(
                self._endpoint(),
                json=payload,
                auth=auth,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise BtcWalletError(f"BTC RPC transport error: {exc}") from exc
        if not isinstance(data, dict):
            raise BtcWalletError("BTC RPC returned non-object")
        if data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else err
            code = err.get("code") if isinstance(err, dict) else ""
            raise BtcWalletRejected(f"{code}: {message}")
        return data.get("result")

    def getnewaddress(self, *, label: str = "") -> str:
        if not self.enabled:
            # Deterministic-looking dry-run bech32-ish placeholder (not spendable).
            return "bcrt1q" + secrets.token_hex(16)
        label = (label or "pdc-funding").strip()[:64]
        addr = self._call("getnewaddress", [label, "bech32"])
        if not isinstance(addr, str) or not addr:
            raise BtcWalletError("getnewaddress returned empty")
        return addr

    def get_received_by_address(self, address: str, minconf: int = 1) -> int:
        """Return amount received at address in satoshis."""
        if not self.enabled:
            return 0
        btc = self._call("getreceivedbyaddress", [address, int(minconf)])
        # Core returns BTC float; convert carefully via string.
        from decimal import Decimal

        sats = (Decimal(str(btc)) * Decimal(100_000_000)).to_integral_value()
        return int(sats)

    def list_received_by_address(self, minconf: int = 1) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        rows = self._call("listreceivedbyaddress", [int(minconf), True, True])
        return rows if isinstance(rows, list) else []

    def get_balance_sats(self) -> int:
        if not self.enabled:
            return 0
        from decimal import Decimal

        btc = self._call("getbalance")
        return int((Decimal(str(btc)) * Decimal(100_000_000)).to_integral_value())

    def send_to_address(
        self,
        address: str,
        amount_sats: int,
        *,
        comment: str = "",
        subtract_fee_from_amount: bool = True,
    ) -> str:
        """Send sats. Default: miner fee comes from amount (escrow-funded hard model)."""
        if amount_sats <= 0:
            raise ValueError("BTC amount must be positive")
        if not self.enabled:
            return f"btc_dryrun_{secrets.token_hex(16)}"
        from decimal import Decimal

        # Pass BTC as a decimal string — never float — to avoid binary rounding.
        btc = (Decimal(amount_sats) / Decimal(100_000_000)).quantize(Decimal("0.00000001"))
        txid = self._call(
            "sendtoaddress",
            [
                address,
                format(btc, "f"),
                comment[:100],
                "",
                bool(subtract_fee_from_amount),
                True,
            ],
        )
        if not isinstance(txid, str) or not txid:
            raise BtcWalletError("sendtoaddress missing txid")
        return txid

    def get_blockchain_info(self) -> dict[str, Any]:
        if not self.enabled:
            return {"blocks": 0}
        # Use non-wallet endpoint for chain info.
        base = self.settings.btc_rpc_url.rstrip("/")
        payload = {
            "jsonrpc": "1.0",
            "id": "pdc-funding",
            "method": "getblockchaininfo",
            "params": [],
        }
        auth = None
        if self.settings.btc_rpc_user:
            auth = (self.settings.btc_rpc_user, self.settings.btc_rpc_password)
        try:
            resp = self._session.post(base, json=payload, auth=auth, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise BtcWalletError(f"BTC RPC transport error: {exc}") from exc
        if data.get("error"):
            raise BtcWalletRejected(str(data["error"]))
        result = data.get("result") or {}
        return result if isinstance(result, dict) else {}
