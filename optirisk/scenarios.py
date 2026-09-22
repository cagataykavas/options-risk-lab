from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from .black_scholes import EuropeanOption, greeks, price
from .portfolio import OptionPortfolio, OptionPosition


def scenario_grid(
    option: EuropeanOption,
    *,
    spot_shocks: Iterable[float] = (-0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15),
    vol_shocks: Iterable[float] = (-0.10, -0.05, 0.0, 0.05, 0.10),
    rate_shocks: Iterable[float] = (-0.01, 0.0, 0.01),
    elapsed_days: Iterable[int] = (0, 5, 20),
) -> list[dict[str, float]]:
    base = price(option)
    rows: list[dict[str, float]] = []
    for spot_shock in spot_shocks:
        for vol_shock in vol_shocks:
            for rate_shock in rate_shocks:
                for days in elapsed_days:
                    maturity = max(1e-6, option.maturity_years - days / 365.0)
                    shocked = replace(
                        option,
                        spot=max(1e-8, option.spot * (1.0 + spot_shock)),
                        volatility=max(1e-6, option.volatility + vol_shock),
                        rate=option.rate + rate_shock,
                        maturity_years=maturity,
                    )
                    shocked_price = price(shocked)
                    rows.append(
                        {
                            "spot_shock": float(spot_shock),
                            "vol_shock": float(vol_shock),
                            "rate_shock": float(rate_shock),
                            "elapsed_days": float(days),
                            "price": float(shocked_price),
                            "pnl": float(shocked_price - base),
                        }
                    )
    return rows


def delta_gamma_vega_approximation(
    option: EuropeanOption,
    *,
    spot_shock: float,
    vol_shock: float,
    elapsed_days: int = 0,
) -> dict[str, float]:
    """Compare a local Greek approximation with full repricing.

    `spot_shock` is a fractional move such as `-0.05`; `vol_shock` is an absolute
    volatility change such as `0.03` for +3 volatility points.
    """
    g = greeks(option)
    ds = option.spot * spot_shock
    vol_points = vol_shock * 100.0
    theta_days = float(elapsed_days)
    approximate = (
        g["delta"] * ds + 0.5 * g["gamma"] * ds * ds + g["vega_1pct"] * vol_points + g["theta_day"] * theta_days
    )
    shocked = replace(
        option,
        spot=max(1e-8, option.spot + ds),
        volatility=max(1e-6, option.volatility + vol_shock),
        maturity_years=max(1e-6, option.maturity_years - elapsed_days / 365.0),
    )
    exact = price(shocked) - price(option)
    return {
        "exact_pnl": float(exact),
        "delta_gamma_vega_theta_approximation": float(approximate),
        "approximation_error": float(approximate - exact),
    }


def portfolio_spot_vol_scenario(
    portfolio: OptionPortfolio,
    *,
    spot_shock: float,
    vol_shock: float,
) -> dict[str, Any]:
    base = portfolio.valuation()["totals"]["market_value"]
    shocked_positions: list[OptionPosition] = []
    for position in portfolio.positions:
        shocked_positions.append(
            OptionPosition(
                instrument_id=position.instrument_id,
                option=replace(
                    position.option,
                    spot=max(1e-8, position.option.spot * (1.0 + spot_shock)),
                    volatility=max(1e-6, position.option.volatility + vol_shock),
                ),
                quantity=position.quantity,
                contract_multiplier=position.contract_multiplier,
            )
        )
    shocked = OptionPortfolio(shocked_positions).valuation()["totals"]["market_value"]
    return {
        "spot_shock": spot_shock,
        "vol_shock": vol_shock,
        "base_market_value": base,
        "shocked_market_value": shocked,
        "pnl": shocked - base,
    }
