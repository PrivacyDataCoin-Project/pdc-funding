"""Environment configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    return int(raw)


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    if not raw:
        return default
    return float(raw)


CATEGORIES = ("wallets", "marketing", "core", "misc", "design", "docs")

# status code → label (FFS lineage)
STATUSES = {
    0: "disabled",
    1: "proposal",
    2: "funding",
    3: "wip",
    4: "completed",
}


@dataclass(frozen=True)
class Settings:
    secret_key: str = field(
        default_factory=lambda: _env("SECRET_KEY", "dev-only-change-me")
    )
    bind_host: str = field(default_factory=lambda: _env("BIND_HOST", "127.0.0.1"))
    bind_port: int = field(default_factory=lambda: _env_int("BIND_PORT", 8088))
    site_name: str = field(default_factory=lambda: _env("SITE_NAME", "PDC Funding"))
    database_path: Path = field(
        default_factory=lambda: Path(_env("DATABASE_PATH", "data/funding.db"))
    )
    wallet_rpc_url: str = field(default_factory=lambda: _env("WALLET_RPC_URL"))
    wallet_rpc_user: str = field(default_factory=lambda: _env("WALLET_RPC_USER"))
    wallet_rpc_password: str = field(
        default_factory=lambda: _env("WALLET_RPC_PASSWORD")
    )
    daemon_rpc_url: str = field(
        default_factory=lambda: _env(
            "DAEMON_RPC_URL", "http://127.0.0.1:19211"
        ).rstrip("/")
    )
    pdc_asset_id: str = field(
        default_factory=lambda: _env(
            "PDC_ASSET_ID",
            "d6329b5b1f7c0805b5c345f4957554002a2f557845f64d7645dae0e051a6498a",
        )
    )
    transfer_fee_atomic: int = field(
        default_factory=lambda: _env_int("TRANSFER_FEE_ATOMIC", 10_000_000_000)
    )
    deposit_min_confirmations: int = field(
        default_factory=lambda: _env_int("DEPOSIT_MIN_CONFIRMATIONS", 10)
    )
    deposit_scan_interval_sec: float = field(
        default_factory=lambda: _env_float("DEPOSIT_SCAN_INTERVAL_SEC", 30.0)
    )
    prices_usd_url: str = field(
        default_factory=lambda: _env(
            "PRICES_USD_URL",
            "https://api.trade.privacydatacoin.com/api/prices/usd",
        )
    )
    explorer_tx_url: str = field(
        default_factory=lambda: _env(
            "EXPLORER_TX_URL",
            "https://explorer.privacydatacoin.com/transaction/",
        )
    )
    bootstrap_admin_user: str = field(
        default_factory=lambda: _env("BOOTSTRAP_ADMIN_USER", "admin")
    )
    bootstrap_admin_password: str = field(
        default_factory=lambda: _env("BOOTSTRAP_ADMIN_PASSWORD")
    )
    bootstrap_admin_email: str = field(
        default_factory=lambda: _env("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    )
    session_https_only: bool = field(
        default_factory=lambda: _env("SESSION_HTTPS_ONLY", "0").lower()
        in ("1", "true", "yes", "on")
    )

    def __post_init__(self) -> None:
        if self.bind_port < 1 or self.bind_port > 65535:
            raise ValueError("BIND_PORT must be between 1 and 65535")
        if self.transfer_fee_atomic < 0:
            raise ValueError("TRANSFER_FEE_ATOMIC must be >= 0")
        if self.deposit_min_confirmations < 0:
            raise ValueError("DEPOSIT_MIN_CONFIRMATIONS must be >= 0")
        if self.deposit_scan_interval_sec <= 0:
            raise ValueError("DEPOSIT_SCAN_INTERVAL_SEC must be > 0")

    @property
    def wallet_enabled(self) -> bool:
        return bool(self.wallet_rpc_url)


def get_settings() -> Settings:
    return Settings()
