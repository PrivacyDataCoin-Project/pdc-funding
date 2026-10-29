from pathlib import Path

import requests

from funding.config import Settings
from funding.prices import fetch_prices, reset_price_cache


def test_price_failure_backs_off(monkeypatch, tmp_path: Path):
    calls = {"n": 0}

    def boom(*_args, **_kwargs):
        calls["n"] += 1
        raise requests.RequestException("down")

    monkeypatch.setattr("funding.prices.requests.get", boom)
    reset_price_cache()
    settings = Settings(
        secret_key="test-secret",
        database_path=tmp_path / "unused.db",
        prices_usd_url="http://127.0.0.1:9/prices",
    )
    assert fetch_prices(settings) is None
    assert fetch_prices(settings) is None
    assert calls["n"] == 1
    reset_price_cache()


def test_public_bind_rejects_placeholder_secret(tmp_path: Path):
    from funding.__main__ import ensure_bind_allowed

    settings = Settings(
        secret_key="dev-only-change-me",
        database_path=tmp_path / "unused.db",
        bind_host="0.0.0.0",
    )
    try:
        ensure_bind_allowed(settings)
        assert False
    except SystemExit as exc:
        assert "SECRET_KEY" in str(exc)

    ensure_bind_allowed(
        Settings(
            secret_key="dev-only-change-me",
            database_path=tmp_path / "unused.db",
            bind_host="127.0.0.1",
        )
    )


def test_negative_confirmations_rejected(tmp_path: Path):
    try:
        Settings(
            secret_key="test-secret",
            database_path=tmp_path / "unused.db",
            deposit_min_confirmations=-1,
        )
        assert False
    except ValueError as exc:
        assert "DEPOSIT_MIN_CONFIRMATIONS" in str(exc)
