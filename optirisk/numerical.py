from __future__ import annotations

from dataclasses import replace

from .black_scholes import EuropeanOption, greeks, price


def finite_difference_greeks(
    option: EuropeanOption,
    *,
    spot_step_fraction: float = 1e-4,
    vol_step: float = 1e-4,
    rate_step: float = 1e-5,
    time_step_years: float = 1.0 / 3650.0,
) -> dict[str, float]:
    ds = max(option.spot * spot_step_fraction, 1e-6)
    up = price(replace(option, spot=option.spot + ds))
    down = price(replace(option, spot=max(1e-8, option.spot - ds)))
    base = price(option)
    delta = (up - down) / (2 * ds)
    gamma = (up - 2 * base + down) / (ds * ds)

    vol_up = price(replace(option, volatility=option.volatility + vol_step))
    vol_down = price(replace(option, volatility=max(1e-8, option.volatility - vol_step)))
    vega_per_unit = (vol_up - vol_down) / (2 * vol_step)

    rate_up = price(replace(option, rate=option.rate + rate_step))
    rate_down = price(replace(option, rate=option.rate - rate_step))
    rho_per_unit = (rate_up - rate_down) / (2 * rate_step)

    shorter = max(1e-8, option.maturity_years - time_step_years)
    theta_day = (price(replace(option, maturity_years=shorter)) - base) / (time_step_years * 365.0)
    return {
        "delta": float(delta),
        "gamma": float(gamma),
        "vega_1pct": float(vega_per_unit / 100.0),
        "theta_day": float(theta_day),
        "rho_1pct": float(rho_per_unit / 100.0),
    }


def greek_verification(option: EuropeanOption) -> dict[str, dict[str, float]]:
    analytic = greeks(option)
    numerical = finite_difference_greeks(option)
    return {
        name: {
            "analytic": float(analytic[name]),
            "finite_difference": float(numerical[name]),
            "absolute_error": float(abs(analytic[name] - numerical[name])),
        }
        for name in analytic
    }


def implied_volatility(
    option: EuropeanOption,
    market_price: float,
    *,
    lower: float = 1e-4,
    upper: float = 5.0,
    tolerance: float = 1e-8,
    max_iterations: int = 200,
) -> float:
    if market_price < 0:
        raise ValueError("market_price must be non-negative")
    low_price = price(replace(option, volatility=lower))
    high_price = price(replace(option, volatility=upper))
    if not low_price - tolerance <= market_price <= high_price + tolerance:
        raise ValueError("market price is outside the configured implied-volatility bracket")

    lo, hi = lower, upper
    for _ in range(max_iterations):
        mid = 0.5 * (lo + hi)
        value = price(replace(option, volatility=mid))
        if abs(value - market_price) <= tolerance:
            return float(mid)
        if value < market_price:
            lo = mid
        else:
            hi = mid
    return float(0.5 * (lo + hi))
