"""Model-free arbitrage checks for one European option expiry slice."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from math import exp, isfinite


@dataclass(frozen=True)
class OptionQuote:
    strike: float
    call_price: float
    put_price: float


@dataclass(frozen=True)
class ArbitragePolicy:
    price_tolerance: float = 1e-6
    parity_tolerance: float = 1e-4
    slope_tolerance: float = 1e-6
    convexity_tolerance: float = 1e-6
    minimum_quotes: int = 3

    def __post_init__(self) -> None:
        tolerances = (
            self.price_tolerance,
            self.parity_tolerance,
            self.slope_tolerance,
            self.convexity_tolerance,
        )
        if any(not isfinite(value) or value < 0 for value in tolerances):
            raise ValueError("policy tolerances must be finite and non-negative")
        if self.minimum_quotes < 3:
            raise ValueError("minimum_quotes must be at least 3")


@dataclass(frozen=True)
class ArbitrageViolation:
    code: str
    option_type: str
    strikes: tuple[float, ...]
    observed: float
    limit: float

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["strikes"] = list(self.strikes)
        return result


@dataclass(frozen=True)
class ArbitrageReport:
    accepted: bool
    quote_count: int
    violations: tuple[ArbitrageViolation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "quote_count": self.quote_count,
            "violation_count": len(self.violations),
            "violations": [violation.to_dict() for violation in self.violations],
        }


def audit_quote_slice(
    quotes: Iterable[OptionQuote],
    *,
    spot: float,
    expiry_years: float,
    rate: float,
    dividend_yield: float = 0.0,
    policy: ArbitragePolicy | None = None,
) -> ArbitrageReport:
    """Audit paired call/put quotes without relying on a pricing model.

    The slice must contain one quote pair per unique strike for a single expiry.
    All violations are collected in deterministic order for release-gate evidence.
    """
    policy = policy or ArbitragePolicy()
    values = (spot, expiry_years, rate, dividend_yield)
    if any(not isfinite(value) for value in values):
        raise ValueError("market inputs must be finite")
    if spot <= 0 or expiry_years <= 0:
        raise ValueError("spot and expiry_years must be positive")

    ordered = sorted(quotes, key=lambda quote: quote.strike)
    if len(ordered) < policy.minimum_quotes:
        raise ValueError(f"at least {policy.minimum_quotes} quotes are required")

    strikes = [quote.strike for quote in ordered]
    if any(not isfinite(value) or value <= 0 for value in strikes):
        raise ValueError("strikes must be finite and positive")
    if len(set(strikes)) != len(strikes):
        raise ValueError("strikes must be unique")
    for quote in ordered:
        if not isfinite(quote.call_price) or not isfinite(quote.put_price):
            raise ValueError("quote prices must be finite")
        if quote.call_price < 0 or quote.put_price < 0:
            raise ValueError("quote prices must be non-negative")

    try:
        discount = exp(-rate * expiry_years)
        dividend_discount = exp(-dividend_yield * expiry_years)
    except OverflowError as exc:
        raise ValueError("discount factors must be representable") from exc
    if not isfinite(discount) or not isfinite(dividend_discount):
        raise ValueError("discount factors must be finite")
    discounted_spot = spot * dividend_discount
    violations: list[ArbitrageViolation] = []

    for quote in ordered:
        discounted_strike = quote.strike * discount
        call_lower = max(0.0, discounted_spot - discounted_strike)
        put_lower = max(0.0, discounted_strike - discounted_spot)
        _check_bound(violations, "call", quote.strike, quote.call_price, call_lower, discounted_spot, policy)
        _check_bound(
            violations,
            "put",
            quote.strike,
            quote.put_price,
            put_lower,
            discounted_strike,
            policy,
        )

        parity_error = abs((quote.call_price - quote.put_price) - (discounted_spot - discounted_strike))
        if parity_error > policy.parity_tolerance:
            violations.append(
                ArbitrageViolation(
                    code="put_call_parity",
                    option_type="pair",
                    strikes=(quote.strike,),
                    observed=parity_error,
                    limit=policy.parity_tolerance,
                )
            )

    for left, right in zip(ordered, ordered[1:], strict=False):
        width = right.strike - left.strike
        call_slope = (right.call_price - left.call_price) / width
        put_slope = (right.put_price - left.put_price) / width
        _check_slope(violations, "call", (left.strike, right.strike), call_slope, -discount, 0.0, policy)
        _check_slope(violations, "put", (left.strike, right.strike), put_slope, 0.0, discount, policy)

    for first, middle, last in zip(ordered, ordered[1:], ordered[2:], strict=False):
        left_width = middle.strike - first.strike
        right_width = last.strike - middle.strike
        for option_type, prices in (
            ("call", (first.call_price, middle.call_price, last.call_price)),
            ("put", (first.put_price, middle.put_price, last.put_price)),
        ):
            left_slope = (prices[1] - prices[0]) / left_width
            right_slope = (prices[2] - prices[1]) / right_width
            convexity_gap = left_slope - right_slope
            if convexity_gap > policy.convexity_tolerance:
                violations.append(
                    ArbitrageViolation(
                        code="butterfly_convexity",
                        option_type=option_type,
                        strikes=(first.strike, middle.strike, last.strike),
                        observed=convexity_gap,
                        limit=policy.convexity_tolerance,
                    )
                )

    return ArbitrageReport(
        accepted=not violations,
        quote_count=len(ordered),
        violations=tuple(violations),
    )


def _check_bound(
    violations: list[ArbitrageViolation],
    option_type: str,
    strike: float,
    observed: float,
    lower: float,
    upper: float,
    policy: ArbitragePolicy,
) -> None:
    if observed < lower - policy.price_tolerance:
        violations.append(ArbitrageViolation("price_below_bound", option_type, (strike,), observed, lower))
    if observed > upper + policy.price_tolerance:
        violations.append(ArbitrageViolation("price_above_bound", option_type, (strike,), observed, upper))


def _check_slope(
    violations: list[ArbitrageViolation],
    option_type: str,
    strikes: tuple[float, float],
    observed: float,
    lower: float,
    upper: float,
    policy: ArbitragePolicy,
) -> None:
    if observed < lower - policy.slope_tolerance:
        violations.append(ArbitrageViolation("vertical_slope_below_bound", option_type, strikes, observed, lower))
    if observed > upper + policy.slope_tolerance:
        violations.append(ArbitrageViolation("vertical_slope_above_bound", option_type, strikes, observed, upper))
