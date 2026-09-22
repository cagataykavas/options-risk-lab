from __future__ import annotations

import json
from math import exp

import pytest

from optirisk.arbitrage import ArbitragePolicy, OptionQuote, audit_quote_slice
from optirisk.black_scholes import EuropeanOption, price
from optirisk.cli import main


def valid_quotes() -> list[OptionQuote]:
    spot, expiry, rate = 100.0, 0.5, 0.03
    return [
        OptionQuote(
            strike=strike,
            call_price=price(EuropeanOption(spot, strike, expiry, rate, 0.22, "call")),
            put_price=price(EuropeanOption(spot, strike, expiry, rate, 0.22, "put")),
        )
        for strike in (80.0, 90.0, 100.0, 110.0, 120.0)
    ]


def audit(quotes: list[OptionQuote], **kwargs: object):
    return audit_quote_slice(quotes, spot=100.0, expiry_years=0.5, rate=0.03, **kwargs)


def test_accepts_arbitrage_free_black_scholes_slice() -> None:
    report = audit(valid_quotes())
    assert report.accepted
    assert report.to_dict() == {
        "accepted": True,
        "quote_count": 5,
        "violation_count": 0,
        "violations": [],
    }


def test_rejects_put_call_parity_break() -> None:
    quotes = valid_quotes()
    quotes[2] = OptionQuote(quotes[2].strike, quotes[2].call_price + 0.5, quotes[2].put_price)
    report = audit(quotes)
    assert not report.accepted
    assert "put_call_parity" in {violation.code for violation in report.violations}


def test_rejects_price_outside_discounted_bounds() -> None:
    quotes = valid_quotes()
    quotes[0] = OptionQuote(80.0, 101.0, quotes[0].put_price)
    report = audit(quotes)
    assert any(v.code == "price_above_bound" and v.option_type == "call" for v in report.violations)


def test_rejects_vertical_spread_slope_break() -> None:
    quotes = valid_quotes()
    quotes[2] = OptionQuote(100.0, quotes[1].call_price + 1.0, quotes[2].put_price)
    report = audit(quotes)
    assert any(v.code == "vertical_slope_above_bound" and v.option_type == "call" for v in report.violations)


def test_rejects_butterfly_convexity_break_on_irregular_strikes() -> None:
    discount = exp(-0.03 * 0.5)
    quotes = [
        OptionQuote(90.0, 15.0, 15.0 - 100.0 + 90.0 * discount),
        OptionQuote(100.0, 14.0, 14.0 - 100.0 + 100.0 * discount),
        OptionQuote(125.0, 5.0, 5.0 - 100.0 + 125.0 * discount),
    ]
    report = audit(quotes)
    assert any(v.code == "butterfly_convexity" for v in report.violations)


@pytest.mark.parametrize(
    "quotes, message",
    [
        ([OptionQuote(100.0, 1.0, 1.0)] * 3, "unique"),
        ([OptionQuote(80.0, 1.0, 1.0), OptionQuote(90.0, float("nan"), 1.0), OptionQuote(100.0, 1.0, 1.0)], "finite"),
        ([OptionQuote(80.0, 1.0, 1.0), OptionQuote(90.0, -1.0, 1.0), OptionQuote(100.0, 1.0, 1.0)], "non-negative"),
    ],
)
def test_fails_closed_on_malformed_quotes(quotes: list[OptionQuote], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        audit(quotes)


def test_rejects_insufficient_evidence() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        audit(valid_quotes()[:2])


def test_rejects_invalid_policy() -> None:
    with pytest.raises(ValueError, match="tolerances"):
        ArbitragePolicy(parity_tolerance=float("inf"))


def test_rejects_unrepresentable_discount_factor() -> None:
    with pytest.raises(ValueError, match="discount factors"):
        audit_quote_slice(valid_quotes(), spot=100.0, expiry_years=100.0, rate=-100.0)


def test_cli_returns_policy_exit_code_and_json(tmp_path, capsys) -> None:
    quotes = valid_quotes()
    quotes[2] = OptionQuote(quotes[2].strike, quotes[2].call_price + 0.5, quotes[2].put_price)
    path = tmp_path / "quotes.json"
    path.write_text(
        json.dumps(
            {
                "spot": 100.0,
                "expiry_years": 0.5,
                "rate": 0.03,
                "quotes": [quote.__dict__ for quote in quotes],
            }
        ),
        encoding="utf-8",
    )
    assert main([str(path), "--require-clean"]) == 2
    assert json.loads(capsys.readouterr().out)["accepted"] is False


def test_cli_returns_malformed_input_exit_code(tmp_path, capsys) -> None:
    path = tmp_path / "quotes.json"
    path.write_text("{}", encoding="utf-8")
    assert main([str(path)]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
