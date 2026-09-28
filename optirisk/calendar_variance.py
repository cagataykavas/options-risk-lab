"""Fail-closed calendar-arbitrage audit for an implied-volatility surface.

The audit checks the necessary condition that total implied variance does not
decrease with expiry at a fixed forward-log-moneyness node.  It deliberately
does not interpolate quotes: the producer must supply a governed node id and
prove that every quote assigned to it is within the configured moneyness
tolerance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "calendar-variance-audit/v1"
SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60


class ArtifactError(ValueError):
    """The supplied artifact is malformed or exceeds a resource budget."""


@dataclass(frozen=True)
class CalendarPolicy:
    moneyness_tolerance: float = 1e-6
    variance_tolerance: float = 1e-10
    max_quote_age_seconds: int = 900
    max_future_skew_seconds: int = 30
    min_maturities_per_node: int = 2
    max_quotes: int = 20_000
    max_nodes: int = 512
    max_reported_breaches: int = 50

    def __post_init__(self) -> None:
        finite_positive = {
            "moneyness_tolerance": self.moneyness_tolerance,
            "variance_tolerance": self.variance_tolerance,
        }
        if any(not math.isfinite(v) or v < 0 for v in finite_positive.values()):
            raise ArtifactError("policy tolerances must be finite and non-negative")
        integer_positive = {
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "max_future_skew_seconds": self.max_future_skew_seconds,
            "min_maturities_per_node": self.min_maturities_per_node,
            "max_quotes": self.max_quotes,
            "max_nodes": self.max_nodes,
            "max_reported_breaches": self.max_reported_breaches,
        }
        if any(
            not isinstance(v, int) or isinstance(v, bool) or v <= 0
            for v in integer_positive.values()
        ):
            raise ArtifactError("policy integer budgets must be positive integers")
        if self.min_maturities_per_node < 2:
            raise ArtifactError("min_maturities_per_node must be at least two")


@dataclass(frozen=True)
class SurfaceQuote:
    node_id: str
    expiry: datetime
    strike: float
    forward: float
    implied_volatility: float


@dataclass(frozen=True)
class CalendarBreach:
    node_hash: str
    short_expiry: str
    long_expiry: str
    short_total_variance: float
    long_total_variance: float
    variance_drop: float


@dataclass(frozen=True)
class CalendarVarianceReport:
    schema_version: str
    accepted: bool
    reason_codes: tuple[str, ...]
    quote_count: int
    node_count: int
    comparison_count: int
    breach_count: int
    max_variance_drop: float
    breaches: tuple[CalendarBreach, ...]
    policy_digest: str
    evidence_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ArtifactError(f"{field} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArtifactError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _finite_positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArtifactError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ArtifactError(f"{field} must be finite and positive")
    return result


def _identifier(value: Any, field: str, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ArtifactError(f"{field} must be a non-empty bounded string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ArtifactError(f"{field} contains control characters")
    return value


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _policy_dict(policy: CalendarPolicy) -> dict[str, Any]:
    return asdict(policy)


def _quote_from_mapping(raw: Any) -> SurfaceQuote:
    if not isinstance(raw, Mapping):
        raise ArtifactError("each quote must be an object")
    expected = {"node_id", "expiry", "strike", "forward", "implied_volatility"}
    if set(raw) != expected:
        raise ArtifactError("quote fields do not match the schema")
    return SurfaceQuote(
        node_id=_identifier(raw["node_id"], "node_id", max_length=64),
        expiry=_parse_datetime(raw["expiry"], "expiry"),
        strike=_finite_positive(raw["strike"], "strike"),
        forward=_finite_positive(raw["forward"], "forward"),
        implied_volatility=_finite_positive(raw["implied_volatility"], "implied_volatility"),
    )


def audit_calendar_variance(
    artifact: Mapping[str, Any],
    *,
    policy: CalendarPolicy = CalendarPolicy(),
    now: datetime | None = None,
) -> CalendarVarianceReport:
    """Validate evidence and audit total-variance monotonicity.

    Malformed or incomplete evidence raises :class:`ArtifactError`. A valid
    artifact with calendar-arbitrage breaches returns ``accepted=False``.
    """
    if not isinstance(artifact, Mapping):
        raise ArtifactError("artifact must be an object")
    expected = {"schema_version", "generated_at", "underlying_id", "surface_revision", "quotes"}
    if set(artifact) != expected:
        raise ArtifactError("artifact fields do not match the schema")
    if artifact["schema_version"] != SCHEMA_VERSION:
        raise ArtifactError("unsupported schema_version")

    clock = now or datetime.now(UTC)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ArtifactError("now must include a timezone")
    clock = clock.astimezone(UTC)
    generated_at = _parse_datetime(artifact["generated_at"], "generated_at")
    age = (clock - generated_at).total_seconds()
    if age < -policy.max_future_skew_seconds:
        raise ArtifactError("artifact is future-dated")
    if age > policy.max_quote_age_seconds:
        raise ArtifactError("artifact is stale")

    underlying_id = _identifier(artifact["underlying_id"], "underlying_id")
    surface_revision = _identifier(artifact["surface_revision"], "surface_revision")
    raw_quotes = artifact["quotes"]
    if not isinstance(raw_quotes, list) or not raw_quotes:
        raise ArtifactError("quotes must be a non-empty array")
    if len(raw_quotes) > policy.max_quotes:
        raise ArtifactError("quote budget exceeded")
    quotes = [_quote_from_mapping(raw) for raw in raw_quotes]

    grouped: dict[str, list[tuple[SurfaceQuote, float, float]]] = {}
    seen: set[tuple[str, datetime]] = set()
    anchors: dict[str, float] = {}
    for quote in quotes:
        if quote.expiry <= generated_at:
            raise ArtifactError("expiry must be after generated_at")
        key = (quote.node_id, quote.expiry)
        if key in seen:
            raise ArtifactError("duplicate node and expiry")
        seen.add(key)
        ratio = quote.strike / quote.forward
        if not math.isfinite(ratio) or ratio <= 0:
            raise ArtifactError("forward-moneyness ratio is not finite")
        log_moneyness = math.log(ratio)
        anchor = anchors.setdefault(quote.node_id, log_moneyness)
        if abs(log_moneyness - anchor) > policy.moneyness_tolerance:
            raise ArtifactError("node moneyness mismatch")
        maturity = (quote.expiry - generated_at).total_seconds() / SECONDS_PER_YEAR
        total_variance = quote.implied_volatility * quote.implied_volatility * maturity
        if not math.isfinite(total_variance):
            raise ArtifactError("total implied variance is not finite")
        grouped.setdefault(quote.node_id, []).append((quote, maturity, total_variance))

    if len(grouped) > policy.max_nodes:
        raise ArtifactError("node budget exceeded")
    if any(len(node_quotes) < policy.min_maturities_per_node for node_quotes in grouped.values()):
        raise ArtifactError("insufficient maturities for a node")

    comparisons = 0
    all_breaches: list[CalendarBreach] = []
    max_drop = 0.0
    for node_id in sorted(grouped):
        ordered = sorted(grouped[node_id], key=lambda item: item[0].expiry)
        for short, long in zip(ordered, ordered[1:]):
            comparisons += 1
            variance_drop = short[2] - long[2]
            max_drop = max(max_drop, variance_drop)
            if variance_drop > policy.variance_tolerance:
                all_breaches.append(
                    CalendarBreach(
                        node_hash=_canonical_digest({"node_id": node_id})[:16],
                        short_expiry=short[0].expiry.isoformat().replace("+00:00", "Z"),
                        long_expiry=long[0].expiry.isoformat().replace("+00:00", "Z"),
                        short_total_variance=short[2],
                        long_total_variance=long[2],
                        variance_drop=variance_drop,
                    )
                )

    policy_digest = _canonical_digest(_policy_dict(policy))
    evidence_payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "underlying_digest": _canonical_digest({"underlying_id": underlying_id}),
        "surface_revision": surface_revision,
        "policy_digest": policy_digest,
        "quotes": [
            {
                "node_id": quote.node_id,
                "expiry": quote.expiry.isoformat(),
                "strike": quote.strike,
                "forward": quote.forward,
                "implied_volatility": quote.implied_volatility,
            }
            for quote in sorted(quotes, key=lambda q: (q.node_id, q.expiry))
        ],
    }
    return CalendarVarianceReport(
        schema_version=SCHEMA_VERSION,
        accepted=not all_breaches,
        reason_codes=() if not all_breaches else ("TOTAL_VARIANCE_DECREASE",),
        quote_count=len(quotes),
        node_count=len(grouped),
        comparison_count=comparisons,
        breach_count=len(all_breaches),
        max_variance_drop=max(0.0, max_drop),
        breaches=tuple(all_breaches[: policy.max_reported_breaches]),
        policy_digest=policy_digest,
        evidence_digest=_canonical_digest(evidence_payload),
    )


def _strict_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactError("duplicate JSON key")
        result[key] = value
    return result


def load_artifact(path: Path, *, max_bytes: int = 2_000_000) -> dict[str, Any]:
    if path.stat().st_size > max_bytes:
        raise ArtifactError("input byte budget exceeded")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ArtifactError("non-finite JSON number")),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError("unable to parse input JSON") from exc
    if not isinstance(value, dict):
        raise ArtifactError("artifact must be an object")
    return value


def write_report_atomic(path: Path, report: CalendarVarianceReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_dict(), sort_keys=True, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit calendar total-variance monotonicity")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = audit_calendar_variance(load_artifact(args.artifact))
        write_report_atomic(args.output, report)
    except (ArtifactError, OSError) as exc:
        parser.exit(3, f"invalid artifact: {exc}\n")
    return 0 if report.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
