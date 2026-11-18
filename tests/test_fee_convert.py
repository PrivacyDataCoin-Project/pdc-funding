"""Oracle fee conversion from proposal BTCX/BTC → PDC L1 fee."""

from funding.assets import ASSET_BTCX, parse_amount
from funding.fee_convert import asset_atomic_for_pdc_fee, plan_pdc_fee_cover
from funding.prices import PriceBook


def _book() -> PriceBook:
    return PriceBook(
        updated_at=1.0,
        prices={"PDC": 1.0, "BTCX": 50_000.0, "BTC": 50_000.0},
        source="test",
    )


def test_plan_prefers_pdc_then_btcx():
    fee = 10_000_000_000  # 0.01 PDC
    plan = plan_pdc_fee_cover(
        fee,
        available_pdc=fee,
        available_btcx=10**8,
        available_btc=0,
        book=_book(),
    )
    assert plan.pdc_from_escrow == fee
    assert plan.btcx_from_escrow == 0


def test_plan_converts_btcx_when_pdc_short():
    fee = 10_000_000_000  # 0.01 PDC ≈ $0.01 at PDC=$1 → tiny BTCX
    plan = plan_pdc_fee_cover(
        fee,
        available_pdc=0,
        available_btcx=10**8,  # 1 BTCX
        available_btc=0,
        book=_book(),
    )
    assert plan.pdc_from_escrow == 0
    assert plan.btcx_from_escrow > 0
    assert plan.btcx_from_escrow < 10**8


def test_plan_fails_without_prices_when_short():
    try:
        plan_pdc_fee_cover(
            10_000_000_000,
            available_pdc=0,
            available_btcx=10**8,
            available_btc=0,
            book=None,
        )
        assert False
    except ValueError as exc:
        assert "prices unavailable" in str(exc).lower() or "unavailable" in str(exc).lower()


def test_asset_atomic_for_pdc_fee_positive():
    fee = parse_amount("0.01", "PDC")
    need = asset_atomic_for_pdc_fee(fee, from_asset=ASSET_BTCX, book=_book())
    assert need >= 1
