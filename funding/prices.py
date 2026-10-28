"""USD prices from Trade API (oracle-backed when Trade enables it)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from funding.config import Settings

logger = logging.getLogger("pdc-funding.prices")

_CACHE: Optional["PriceBook"] = None
_CACHE_AT = 0.0
_TTL_SEC = 60.0


@dataclass(frozen=True)
class PriceBook:
    updated_at: float
    prices: dict[str, float]
    source: str = "trade"

    def pdc_usd(self) -> Optional[float]:
        for key in ("PDC", "pdc"):
            val = self.prices.get(key)
            if val is not None and val > 0:
                return float(val)
        return None


def fetch_prices(settings: Settings, *, force: bool = False) -> Optional[PriceBook]:
    global _CACHE, _CACHE_AT
    now = time.time()
    if not force and _CACHE is not None and (now - _CACHE_AT) < _TTL_SEC:
        return _CACHE
    try:
        resp = requests.get(settings.prices_usd_url, timeout=20)
        resp.raise_for_status()
        data: Any = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("prices fetch failed: %s", exc)
        return _CACHE

    raw_prices = data.get("prices") if isinstance(data, dict) else None
    if not isinstance(raw_prices, dict):
        logger.warning("prices response missing prices map")
        return _CACHE

    prices: dict[str, float] = {}
    for k, v in raw_prices.items():
        try:
            prices[str(k).upper()] = float(v)
        except (TypeError, ValueError):
            continue

    source = str(data.get("source") or "trade") if isinstance(data, dict) else "trade"
    book = PriceBook(updated_at=now, prices=prices, source=source)
    _CACHE = book
    _CACHE_AT = now
    return book


def reset_price_cache() -> None:
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0


def atomic_to_usd(atomic: int, pdc_usd: Optional[float]) -> Optional[float]:
    if pdc_usd is None or pdc_usd <= 0 or atomic < 0:
        return None
    return round((atomic / 1_000_000_000_000) * pdc_usd, 2)
