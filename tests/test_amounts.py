from funding.amounts import ATOMIC, MILLI, format_amount, funding_pct, parse_amount


def test_parse_and_format_roundtrip():
    assert parse_amount("1") == ATOMIC
    assert parse_amount("1.5") == ATOMIC + ATOMIC // 2
    assert format_amount(ATOMIC) == "1.000"
    assert format_amount(ATOMIC + ATOMIC // 2) == "1.500"
    assert format_amount(10_000_000_000) == "0.010"
    assert format_amount(MILLI) == "0.001"
    # Sub-milli dust floors to the lower milli for display.
    assert format_amount(MILLI + 1) == "0.001"
    assert format_amount(MILLI - 1) == "0.000"


def test_parse_rejects_bad():
    for bad in ("", "0", "-1", "abc", "nan", "inf", "+inf", "1e30"):
        try:
            parse_amount(bad)
            assert False, bad
        except ValueError:
            pass


def test_funding_pct():
    assert funding_pct(0, ATOMIC) == 0.0
    assert funding_pct(ATOMIC // 2, ATOMIC) == 50.0
    assert funding_pct(ATOMIC * 2, ATOMIC) == 100.0
