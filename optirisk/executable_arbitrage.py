"""Fail-closed executable-arbitrage audit for one European option expiry.

Unlike a mid-price shape check, this module only reports an arbitrage when the
required legs can cross the supplied bid/ask spread and leave a strictly
positive, policy-sized credit after reserving any deterministic terminal
liability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = "1.0"
REPORT_VERSION = "1.0"
EXIT_ACCEPTED = 0
EXIT_REJECTED = 2
EXIT_MALFORMED = 3
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_PRICE_MAGNITUDE = 1e15
_MAX_MATURITY_YEARS = 100.0
_MAX_ABS_CARRY_RATE = 10.0


class ArtifactError(ValueError):
    """The artifact cannot be evaluated safely."""


@dataclass(frozen=True)
class AuditPolicy:
    max_input_bytes: int = 131_072
    max_quotes_per_type: int = 48
    max_strategy_checks: int = 50_000
    max_reported_findings: int = 64
    max_quote_age_seconds: float = 120.0
    max_future_skew_seconds: float = 5.0
    max_relative_spread: float = 0.50
    min_executable_profit: float = 0.01
    numerical_tolerance: float = 1e-10

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_input_bytes,
            self.max_quotes_per_type,
            self.max_strategy_checks,
            self.max_reported_findings,
        )
        if any(type(value) is not int or value <= 0 for value in integer_limits):
            raise ValueError("integer policy limits must be positive integers")
        numeric_limits = (
            self.max_quote_age_seconds,
            self.max_future_skew_seconds,
            self.max_relative_spread,
            self.min_executable_profit,
            self.numerical_tolerance,
        )
        if any(not _is_finite_number(value) or value < 0 for value in numeric_limits):
            raise ValueError("numeric policy limits must be finite and non-negative")
        if self.max_relative_spread > 2.0:
            raise ValueError("max_relative_spread must not exceed 2")


@dataclass(frozen=True)
class Quote:
    option_type: Literal["call", "put"]
    strike: float
    bid: float
    ask: float
    observed_at: datetime


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    maturity_years: float
    rate: float
    dividend_yield: float
    spot_bid: float
    spot_ask: float
    observed_at: datetime
    quotes: tuple[Quote, ...]


def _is_finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


DEFAULT_POLICY = AuditPolicy()


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if not _is_finite_number(value):
        raise ArtifactError(f"{name} must be a finite number")
    result = float(value)
    if positive and result <= 0:
        raise ArtifactError(f"{name} must be positive")
    return result


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ArtifactError(f"{name} must be an RFC 3339 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ArtifactError(f"{name} must be a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArtifactError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _require_keys(value: dict[str, Any], required: set[str], name: str) -> None:
    missing = required - value.keys()
    extra = value.keys() - required
    if missing:
        raise ArtifactError(f"{name} is missing: {', '.join(sorted(missing))}")
    if extra:
        raise ArtifactError(f"{name} has unknown fields: {', '.join(sorted(extra))}")


def _parse_snapshot(payload: Any, policy: AuditPolicy) -> Snapshot:
    if not isinstance(payload, dict):
        raise ArtifactError("root must be an object")
    root_keys = {
        "schema_version",
        "snapshot_id",
        "maturity_years",
        "rate",
        "dividend_yield",
        "spot_bid",
        "spot_ask",
        "observed_at",
        "quotes",
    }
    _require_keys(payload, root_keys, "root")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ArtifactError(f"schema_version must be {SCHEMA_VERSION}")
    snapshot_id = payload["snapshot_id"]
    if not isinstance(snapshot_id, str) or not _ID_RE.fullmatch(snapshot_id):
        raise ArtifactError("snapshot_id has an invalid format")

    maturity = _number(payload["maturity_years"], "maturity_years", positive=True)
    rate = _number(payload["rate"], "rate")
    dividend = _number(payload["dividend_yield"], "dividend_yield")
    if maturity > _MAX_MATURITY_YEARS:
        raise ArtifactError("maturity_years exceeds the safe magnitude limit")
    if abs(rate) > _MAX_ABS_CARRY_RATE or abs(dividend) > _MAX_ABS_CARRY_RATE:
        raise ArtifactError("rate magnitude exceeds the safe limit")
    if abs(rate * maturity) > 700 or abs(dividend * maturity) > 700:
        raise ArtifactError("discount exponent is outside the safe numeric range")
    spot_bid = _number(payload["spot_bid"], "spot_bid", positive=True)
    spot_ask = _number(payload["spot_ask"], "spot_ask", positive=True)
    if max(spot_bid, spot_ask) > _MAX_PRICE_MAGNITUDE:
        raise ArtifactError("spot price exceeds the safe magnitude limit")
    observed_at = _parse_time(payload["observed_at"], "observed_at")
    rows = payload["quotes"]
    if not isinstance(rows, list) or not rows:
        raise ArtifactError("quotes must be a non-empty array")

    counts = {"call": 0, "put": 0}
    seen: set[tuple[str, float]] = set()
    quotes: list[Quote] = []
    quote_keys = {"option_type", "strike", "bid", "ask", "observed_at"}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ArtifactError(f"quotes[{index}] must be an object")
        _require_keys(row, quote_keys, f"quotes[{index}]")
        option_type = row["option_type"]
        if option_type not in ("call", "put"):
            raise ArtifactError(f"quotes[{index}].option_type must be call or put")
        counts[option_type] += 1
        if counts[option_type] > policy.max_quotes_per_type:
            raise ArtifactError(f"too many {option_type} quotes")
        strike = _number(row["strike"], f"quotes[{index}].strike", positive=True)
        bid = _number(row["bid"], f"quotes[{index}].bid")
        ask = _number(row["ask"], f"quotes[{index}].ask")
        if bid < 0 or ask < 0:
            raise ArtifactError(f"quotes[{index}] prices must be non-negative")
        if max(strike, bid, ask) > _MAX_PRICE_MAGNITUDE:
            raise ArtifactError(f"quotes[{index}] exceeds the safe magnitude limit")
        key = (option_type, strike)
        if key in seen:
            raise ArtifactError(f"duplicate {option_type} strike")
        seen.add(key)
        quotes.append(
            Quote(
                option_type=option_type,
                strike=strike,
                bid=bid,
                ask=ask,
                observed_at=_parse_time(
                    row["observed_at"], f"quotes[{index}].observed_at"
                ),
            )
        )
    return Snapshot(
        snapshot_id=snapshot_id,
        maturity_years=maturity,
        rate=rate,
        dividend_yield=dividend,
        spot_bid=spot_bid,
        spot_ask=spot_ask,
        observed_at=observed_at,
        quotes=tuple(quotes),
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _serialized_snapshot(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "maturity_years": snapshot.maturity_years,
        "rate": snapshot.rate,
        "dividend_yield": snapshot.dividend_yield,
        "spot_bid": snapshot.spot_bid,
        "spot_ask": snapshot.spot_ask,
        "observed_at": snapshot.observed_at.isoformat(),
        "quotes": [
            {
                "option_type": quote.option_type,
                "strike": quote.strike,
                "bid": quote.bid,
                "ask": quote.ask,
                "observed_at": quote.observed_at.isoformat(),
            }
            for quote in sorted(
                snapshot.quotes, key=lambda q: (q.option_type, q.strike)
            )
        ],
    }


def _finding(
    code: str,
    *,
    option_type: str | None = None,
    strikes: Iterable[float] = (),
    executable_profit: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "strikes": list(strikes)}
    if option_type is not None:
        result["option_type"] = option_type
    if executable_profit is not None:
        result["executable_profit"] = round(max(0.0, executable_profit), 12)
    return result


def _estimated_strategy_checks(groups: dict[str, list[Quote]]) -> int:
    calls = len(groups["call"])
    puts = len(groups["put"])
    matched = len(
        {q.strike for q in groups["call"]} & {q.strike for q in groups["put"]}
    )
    pairs = 2 * (math.comb(calls, 2) + math.comb(puts, 2))
    butterflies = math.comb(calls, 3) + math.comb(puts, 3)
    return pairs + butterflies + 2 * matched


def audit_snapshot(
    payload: Any,
    *,
    evaluated_at: datetime,
    policy: AuditPolicy = DEFAULT_POLICY,
) -> dict[str, Any]:
    """Audit a parsed JSON payload and return deterministic bounded evidence."""
    if not isinstance(evaluated_at, datetime):
        raise ArtifactError("evaluated_at must be a datetime")
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ArtifactError("evaluated_at must include a timezone")
    now = evaluated_at.astimezone(UTC)
    snapshot = _parse_snapshot(payload, policy)
    groups = {
        kind: sorted(
            (quote for quote in snapshot.quotes if quote.option_type == kind),
            key=lambda quote: quote.strike,
        )
        for kind in ("call", "put")
    }
    strategy_checks = _estimated_strategy_checks(groups)
    if strategy_checks > policy.max_strategy_checks:
        raise ArtifactError("strategy-check budget exceeded")

    findings: list[dict[str, Any]] = []

    def add(item: dict[str, Any]) -> None:
        findings.append(item)

    all_times = [("SNAPSHOT", snapshot.observed_at, None)] + [
        ("QUOTE", quote.observed_at, quote) for quote in snapshot.quotes
    ]
    for source, observed, quote in all_times:
        age = (now - observed).total_seconds()
        detail = (
            {"option_type": quote.option_type, "strikes": [quote.strike]}
            if quote
            else {}
        )
        if age > policy.max_quote_age_seconds:
            add(_finding(f"STALE_{source}", **detail))
        elif age < -policy.max_future_skew_seconds:
            add(_finding(f"FUTURE_{source}", **detail))

    if snapshot.spot_bid > snapshot.spot_ask + policy.numerical_tolerance:
        add(_finding("CROSSED_SPOT_MARKET"))

    for quote in snapshot.quotes:
        if quote.bid > quote.ask + policy.numerical_tolerance:
            add(
                _finding(
                    "CROSSED_OPTION_MARKET",
                    option_type=quote.option_type,
                    strikes=[quote.strike],
                )
            )
            continue
        midpoint = 0.5 * (quote.bid + quote.ask)
        relative_spread = (quote.ask - quote.bid) / max(
            midpoint, policy.numerical_tolerance
        )
        if relative_spread > policy.max_relative_spread + policy.numerical_tolerance:
            add(
                _finding(
                    "EXCESSIVE_RELATIVE_SPREAD",
                    option_type=quote.option_type,
                    strikes=[quote.strike],
                )
            )

    discount = math.exp(-snapshot.rate * snapshot.maturity_years)
    dividend_discount = math.exp(-snapshot.dividend_yield * snapshot.maturity_years)
    hurdle = policy.min_executable_profit + policy.numerical_tolerance

    for option_type, quotes in groups.items():
        for low, high in combinations(quotes, 2):
            width_pv = discount * (high.strike - low.strike)
            if option_type == "call":
                monotonic_profit = high.bid - low.ask
                slope_profit = low.bid - high.ask - width_pv
            else:
                monotonic_profit = low.bid - high.ask
                slope_profit = high.bid - low.ask - width_pv
            if monotonic_profit > hurdle:
                add(
                    _finding(
                        "EXECUTABLE_VERTICAL_MONOTONICITY",
                        option_type=option_type,
                        strikes=[low.strike, high.strike],
                        executable_profit=monotonic_profit,
                    )
                )
            if slope_profit > hurdle:
                add(
                    _finding(
                        "EXECUTABLE_VERTICAL_SLOPE",
                        option_type=option_type,
                        strikes=[low.strike, high.strike],
                        executable_profit=slope_profit,
                    )
                )

        for low, middle, high in combinations(quotes, 3):
            low_weight = (high.strike - middle.strike) / (high.strike - low.strike)
            high_weight = 1.0 - low_weight
            profit = middle.bid - low_weight * low.ask - high_weight * high.ask
            if profit > hurdle:
                add(
                    _finding(
                        "EXECUTABLE_BUTTERFLY_CONVEXITY",
                        option_type=option_type,
                        strikes=[low.strike, middle.strike, high.strike],
                        executable_profit=profit,
                    )
                )

    calls = {quote.strike: quote for quote in groups["call"]}
    puts = {quote.strike: quote for quote in groups["put"]}
    prepaid_bid = snapshot.spot_bid * dividend_discount
    prepaid_ask = snapshot.spot_ask * dividend_discount
    for strike in sorted(calls.keys() & puts.keys()):
        call = calls[strike]
        put = puts[strike]
        bond = strike * discount
        sell_call_side = call.bid + bond - put.ask - prepaid_ask
        sell_put_side = put.bid + prepaid_bid - call.ask - bond
        if sell_call_side > hurdle:
            add(
                _finding(
                    "EXECUTABLE_PUT_CALL_PARITY_CALL_RICH",
                    strikes=[strike],
                    executable_profit=sell_call_side,
                )
            )
        if sell_put_side > hurdle:
            add(
                _finding(
                    "EXECUTABLE_PUT_CALL_PARITY_PUT_RICH",
                    strikes=[strike],
                    executable_profit=sell_put_side,
                )
            )

    findings.sort(
        key=lambda item: (
            item["code"],
            item.get("option_type", ""),
            item["strikes"],
            -item.get("executable_profit", 0.0),
        )
    )
    report = {
        "report_version": REPORT_VERSION,
        "status": "accepted" if not findings else "rejected",
        "evaluated_at": now.isoformat(),
        "snapshot_sha256": _digest(_serialized_snapshot(snapshot)),
        "policy_sha256": _digest(asdict(policy)),
        "summary": {
            "call_quotes": len(groups["call"]),
            "put_quotes": len(groups["put"]),
            "strategy_checks": strategy_checks,
            "finding_count": len(findings),
            "reported_finding_count": min(len(findings), policy.max_reported_findings),
            "findings_truncated": len(findings) > policy.max_reported_findings,
        },
        "findings": findings[: policy.max_reported_findings],
    }
    report["evidence_sha256"] = _digest(report)
    return report


def _reject_constant(value: str) -> None:
    raise ArtifactError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def load_artifact(path: Path, policy: AuditPolicy = DEFAULT_POLICY) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ArtifactError("cannot stat input artifact") from exc
    if size > policy.max_input_bytes:
        raise ArtifactError("input artifact exceeds byte budget")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArtifactError("cannot read input artifact") from exc
    if len(raw) > policy.max_input_bytes:
        raise ArtifactError("input artifact exceeds byte budget")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError("input artifact must be UTF-8") from exc
    try:
        return json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise ArtifactError("input artifact is not valid JSON") from exc


def write_atomic(path: Path | None, report: dict[str, Any]) -> None:
    output = json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(output, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="quote snapshot JSON")
    parser.add_argument("--output", type=Path, help="atomic JSON report destination")
    parser.add_argument(
        "--evaluated-at",
        help="RFC 3339 evaluation time; defaults to current UTC time",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evaluated_at = (
            _parse_time(args.evaluated_at, "evaluated_at")
            if args.evaluated_at
            else datetime.now(UTC)
        )
        report = audit_snapshot(load_artifact(args.input), evaluated_at=evaluated_at)
        write_atomic(args.output, report)
    except (ArtifactError, OSError) as exc:
        print(json.dumps({"status": "malformed", "error": str(exc)}, sort_keys=True))
        return EXIT_MALFORMED
    return EXIT_ACCEPTED if report["status"] == "accepted" else EXIT_REJECTED


if __name__ == "__main__":
    raise SystemExit(main())
