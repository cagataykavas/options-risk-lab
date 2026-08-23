from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .black_scholes import EuropeanOption, greeks, price


@dataclass(frozen=True)
class OptionPosition:
    instrument_id: str
    option: EuropeanOption
    quantity: float = 1.0
    contract_multiplier: float = 100.0

    @property
    def scale(self) -> float:
        return self.quantity * self.contract_multiplier


class OptionPortfolio:
    def __init__(self, positions: list[OptionPosition]) -> None:
        if not positions:
            raise ValueError("portfolio requires at least one position")
        self.positions = positions

    def valuation(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        totals = {"market_value": 0.0, "delta": 0.0, "gamma": 0.0, "vega_1pct": 0.0, "theta_day": 0.0, "rho_1pct": 0.0}
        for position in self.positions:
            unit_price = price(position.option)
            unit_greeks = greeks(position.option)
            scale = position.scale
            row = {
                "instrument_id": position.instrument_id,
                "quantity": position.quantity,
                "contract_multiplier": position.contract_multiplier,
                "unit_price": unit_price,
                "market_value": unit_price * scale,
                **{name: value * scale for name, value in unit_greeks.items()},
            }
            rows.append(row)
            totals["market_value"] += row["market_value"]
            for name in ("delta", "gamma", "vega_1pct", "theta_day", "rho_1pct"):
                totals[name] += row[name]
        return {"positions": rows, "totals": totals}
