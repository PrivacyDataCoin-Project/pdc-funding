from funding.amounts import ATOMIC, format_amount, funding_pct, parse_amount


def test_parse_and_format_roundtrip():
    assert parse_amount("1") == ATOMIC
    assert parse_amount("1.5") == ATOMIC + ATOMIC // 2
    assert format_amount(ATOMIC) == "1"
    assert format_amount(ATOMIC + ATOMIC // 2) == "1.5"


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
