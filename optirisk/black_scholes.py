from __future__ import annotations

from dataclasses import dataclass, replace
from math import erf, exp, log, pi, sqrt
from typing import Literal

OptionType = Literal["call", "put"]


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


@dataclass(frozen=True)
class EuropeanOption:
    spot: float
    strike: float
    maturity_years: float
    rate: float
    volatility: float
    option_type: OptionType = "call"
    dividend_yield: float = 0.0

    def __post_init__(self) -> None:
        if self.spot <= 0 or self.strike <= 0:
            raise ValueError("spot and strike must be positive")
        if self.maturity_years <= 0:
            raise ValueError("maturity_years must be positive")
        if self.volatility <= 0:
            raise ValueError("volatility must be positive")
        if self.option_type not in {"call", "put"}:
            raise ValueError("option_type must be call or put")

    def bumped(self, **changes: float | str) -> "EuropeanOption":
        return replace(self, **changes)


def d1_d2(option: EuropeanOption) -> tuple[float, float]:
    sqrt_t = sqrt(option.maturity_years)
    numerator = log(option.spot / option.strike) + (
        option.rate - option.dividend_yield + 0.5 * option.volatility**2
    ) * option.maturity_years
    d1 = numerator / (option.volatility * sqrt_t)
    return d1, d1 - option.volatility * sqrt_t


def price(option: EuropeanOption) -> float:
    d1, d2 = d1_d2(option)
    df_r = exp(-option.rate * option.maturity_years)
    df_q = exp(-option.dividend_yield * option.maturity_years)
    if option.option_type == "call":
        return option.spot * df_q * norm_cdf(d1) - option.strike * df_r * norm_cdf(d2)
    return option.strike * df_r * norm_cdf(-d2) - option.spot * df_q * norm_cdf(-d1)


def greeks(option: EuropeanOption) -> dict[str, float]:
    """Return analytic Greeks in common desk-friendly units.

    Vega and rho are reported per one percentage-point change. Theta is per calendar day.
    """
    d1, d2 = d1_d2(option)
    t = option.maturity_years
    sqrt_t = sqrt(t)
    df_r = exp(-option.rate * t)
    df_q = exp(-option.dividend_yield * t)
    pdf = norm_pdf(d1)

    if option.option_type == "call":
        delta = df_q * norm_cdf(d1)
        theta_year = (
            -(option.spot * df_q * pdf * option.volatility) / (2 * sqrt_t)
            - option.rate * option.strike * df_r * norm_cdf(d2)
            + option.dividend_yield * option.spot * df_q * norm_cdf(d1)
        )
        rho = option.strike * t * df_r * norm_cdf(d2)
    else:
        delta = df_q * (norm_cdf(d1) - 1.0)
        theta_year = (
            -(option.spot * df_q * pdf * option.volatility) / (2 * sqrt_t)
            + option.rate * option.strike * df_r * norm_cdf(-d2)
            - option.dividend_yield * option.spot * df_q * norm_cdf(-d1)
        )
        rho = -option.strike * t * df_r * norm_cdf(-d2)

    gamma = df_q * pdf / (option.spot * option.volatility * sqrt_t)
    vega = option.spot * df_q * pdf * sqrt_t
    return {
        "delta": float(delta),
        "gamma": float(gamma),
        "vega_1pct": float(vega / 100.0),
        "theta_day": float(theta_year / 365.0),
        "rho_1pct": float(rho / 100.0),
    }


def put_call_parity_error(call: EuropeanOption, put: EuropeanOption) -> float:
    if call.option_type != "call" or put.option_type != "put":
        raise ValueError("expected one call and one put")
    attributes = ("spot", "strike", "maturity_years", "rate", "volatility", "dividend_yield")
    if any(getattr(call, name) != getattr(put, name) for name in attributes):
        raise ValueError("call and put must share contract parameters")
    left = price(call) - price(put)
    right = (
        call.spot * exp(-call.dividend_yield * call.maturity_years)
        - call.strike * exp(-call.rate * call.maturity_years)
    )
    return float(left - right)
