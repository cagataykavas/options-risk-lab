from __future__ import annotations

from dataclasses import dataclass
from math import erf, exp, log, pi, sqrt


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2.0 * pi)


@dataclass(frozen=True)
class Option:
    spot: float
    strike: float
    maturity: float
    rate: float
    volatility: float
    option_type: str = "call"


def d1_d2(o: Option) -> tuple[float, float]:
    d1 = (log(o.spot / o.strike) + (o.rate + 0.5 * o.volatility**2) * o.maturity) / (o.volatility * sqrt(o.maturity))
    return d1, d1 - o.volatility * sqrt(o.maturity)


def price(o: Option) -> float:
    d1, d2 = d1_d2(o)
    discount = exp(-o.rate * o.maturity)
    if o.option_type == "call":
        return o.spot * norm_cdf(d1) - o.strike * discount * norm_cdf(d2)
    return o.strike * discount * norm_cdf(-d2) - o.spot * norm_cdf(-d1)


def greeks(o: Option) -> dict[str, float]:
    d1, d2 = d1_d2(o)
    pdf = norm_pdf(d1)
    discount = exp(-o.rate * o.maturity)
    sign = 1 if o.option_type == "call" else -1
    delta = norm_cdf(d1) if sign == 1 else norm_cdf(d1) - 1
    gamma = pdf / (o.spot * o.volatility * sqrt(o.maturity))
    vega = o.spot * pdf * sqrt(o.maturity) / 100
    theta_common = -(o.spot * pdf * o.volatility) / (2 * sqrt(o.maturity))
    theta = (theta_common - sign * o.rate * o.strike * discount * norm_cdf(sign * d2)) / 365
    rho = sign * o.strike * o.maturity * discount * norm_cdf(sign * d2) / 100
    return {"delta": delta, "gamma": gamma, "vega_1pct": vega, "theta_day": theta, "rho_1pct": rho}


def shock_grid(o: Option, spot_shocks=(-0.1, -0.05, 0, 0.05, 0.1), vol_shocks=(-0.05, 0, 0.05)):
    base = price(o)
    rows = []
    for ds in spot_shocks:
        for dv in vol_shocks:
            shocked = Option(o.spot * (1 + ds), o.strike, o.maturity, o.rate, max(0.001, o.volatility + dv), o.option_type)
            p = price(shocked)
            rows.append({"spot_shock": ds, "vol_shock": dv, "price": p, "pnl": p - base})
    return rows


if __name__ == "__main__":
    contract = Option(spot=100, strike=105, maturity=0.5, rate=0.04, volatility=0.22)
    print("price", round(price(contract), 4))
    print("greeks", {k: round(v, 5) for k, v in greeks(contract).items()})
    for row in shock_grid(contract):
        print(row)
