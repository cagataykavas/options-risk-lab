from __future__ import annotations

import argparse
import json
import math
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .black_scholes import EuropeanOption, price
from .scenarios import delta_gamma_vega_approximation

MAX_POLICY_BYTES = 1_000_000
MAX_GRID_POINTS = 1_000


class ApproximationAuditInputError(ValueError):
    """Raised when a validation policy is unsafe or internally inconsistent."""


@dataclass(frozen=True)
class ShockPointResult:
    spot_shock: float
    vol_shock: float
    elapsed_days: int
    exact_pnl: float
    approximate_pnl: float
    approximation_error: float
    allowed_error: float
    tolerance_ratio: float
    passed: bool


@dataclass(frozen=True)
class ApproximationAuditReport:
    base_price: float
    absolute_tolerance: float
    relative_tolerance: float
    total_points: int
    passed_points: int
    maximum_absolute_error: float
    maximum_tolerance_ratio: float
    points: tuple[ShockPointResult, ...]

    @property
    def ok(self) -> bool:
        return self.total_points > 0 and self.passed_points == self.total_points

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "base_price": self.base_price,
            "absolute_tolerance": self.absolute_tolerance,
            "relative_tolerance": self.relative_tolerance,
            "total_points": self.total_points,
            "passed_points": self.passed_points,
            "maximum_absolute_error": self.maximum_absolute_error,
            "maximum_tolerance_ratio": self.maximum_tolerance_ratio,
            "points": [asdict(point) for point in self.points],
        }


@dataclass(frozen=True)
class ApproximationPolicy:
    option: EuropeanOption
    spot_shocks: tuple[float, ...]
    vol_shocks: tuple[float, ...]
    elapsed_days: tuple[int, ...]
    absolute_tolerance: float
    relative_tolerance: float


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ApproximationAuditInputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_policy_json(path: str | Path) -> Any:
    source = Path(path)
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise ApproximationAuditInputError(f"cannot inspect policy: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ApproximationAuditInputError("policy must be a regular, non-symlink file")
    if metadata.st_size > MAX_POLICY_BYTES:
        raise ApproximationAuditInputError(f"policy exceeds {MAX_POLICY_BYTES} bytes")
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise ApproximationAuditInputError(f"policy exceeds {MAX_POLICY_BYTES} bytes")
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ApproximationAuditInputError(f"non-finite JSON number: {value}")
            ),
        )
    except UnicodeDecodeError as exc:
        raise ApproximationAuditInputError("policy is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ApproximationAuditInputError(f"invalid policy JSON: {exc.msg}") from exc
    except OSError as exc:
        raise ApproximationAuditInputError(f"cannot read policy: {exc}") from exc


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApproximationAuditInputError(f"{field} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ApproximationAuditInputError(f"{field} must be a finite number")
    return parsed


def _bounded_number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    parsed = _finite_number(value, field)
    if parsed < minimum or parsed > maximum:
        raise ApproximationAuditInputError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return parsed


def _float_grid(value: Any, field: str, *, minimum: float, maximum: float) -> tuple[float, ...]:
    if not isinstance(value, list) or not value:
        raise ApproximationAuditInputError(f"{field} must be a non-empty array")
    parsed = tuple(_finite_number(item, field) for item in value)
    if len(set(parsed)) != len(parsed):
        raise ApproximationAuditInputError(f"{field} must not contain duplicates")
    if any(item < minimum or item > maximum for item in parsed):
        raise ApproximationAuditInputError(
            f"{field} values must be between {minimum} and {maximum}"
        )
    return parsed


def parse_policy(payload: Any) -> ApproximationPolicy:
    if not isinstance(payload, dict):
        raise ApproximationAuditInputError("policy must be a JSON object")
    option_payload = payload.get("option")
    if not isinstance(option_payload, dict):
        raise ApproximationAuditInputError("option must be a JSON object")

    option_type = option_payload.get("option_type", "call")
    if option_type not in {"call", "put"}:
        raise ApproximationAuditInputError("option.option_type must be call or put")
    option = EuropeanOption(
        spot=_bounded_number(
            option_payload.get("spot"), "option.spot", minimum=1e-9, maximum=1e12
        ),
        strike=_bounded_number(
            option_payload.get("strike"), "option.strike", minimum=1e-9, maximum=1e12
        ),
        maturity_years=_bounded_number(
            option_payload.get("maturity_years"),
            "option.maturity_years",
            minimum=1e-9,
            maximum=100.0,
        ),
        rate=_bounded_number(
            option_payload.get("rate"), "option.rate", minimum=-1.0, maximum=1.0
        ),
        volatility=_bounded_number(
            option_payload.get("volatility"),
            "option.volatility",
            minimum=1e-6,
            maximum=10.0,
        ),
        option_type=option_type,
        dividend_yield=_bounded_number(
            option_payload.get("dividend_yield", 0.0),
            "option.dividend_yield",
            minimum=-1.0,
            maximum=1.0,
        ),
    )
    spot_shocks = _float_grid(
        payload.get("spot_shocks"), "spot_shocks", minimum=-0.50, maximum=0.50
    )
    vol_shocks = _float_grid(
        payload.get("vol_shocks"), "vol_shocks", minimum=-1.0, maximum=1.0
    )
    if any(option.volatility + shock <= 0 for shock in vol_shocks):
        raise ApproximationAuditInputError(
            "vol_shocks must keep candidate volatility strictly positive"
        )

    elapsed_raw = payload.get("elapsed_days")
    if not isinstance(elapsed_raw, list) or not elapsed_raw:
        raise ApproximationAuditInputError("elapsed_days must be a non-empty array")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in elapsed_raw):
        raise ApproximationAuditInputError("elapsed_days values must be integers")
    elapsed_days = tuple(elapsed_raw)
    if len(set(elapsed_days)) != len(elapsed_days):
        raise ApproximationAuditInputError("elapsed_days must not contain duplicates")
    if any(day < 0 for day in elapsed_days):
        raise ApproximationAuditInputError("elapsed_days values must be non-negative")
    if any(day >= option.maturity_years * 365 for day in elapsed_days):
        raise ApproximationAuditInputError("elapsed_days must remain before option expiry")

    absolute_tolerance = _finite_number(
        payload.get("absolute_tolerance"), "absolute_tolerance"
    )
    relative_tolerance = _finite_number(
        payload.get("relative_tolerance"), "relative_tolerance"
    )
    if absolute_tolerance < 0 or relative_tolerance < 0:
        raise ApproximationAuditInputError("tolerances must be non-negative")
    if absolute_tolerance == 0 and relative_tolerance == 0:
        raise ApproximationAuditInputError("at least one tolerance must be positive")

    grid_points = len(spot_shocks) * len(vol_shocks) * len(elapsed_days)
    if grid_points > MAX_GRID_POINTS:
        raise ApproximationAuditInputError(
            f"shock grid has {grid_points} points; maximum is {MAX_GRID_POINTS}"
        )
    return ApproximationPolicy(
        option=option,
        spot_shocks=spot_shocks,
        vol_shocks=vol_shocks,
        elapsed_days=elapsed_days,
        absolute_tolerance=absolute_tolerance,
        relative_tolerance=relative_tolerance,
    )


def audit_approximation(policy: ApproximationPolicy) -> ApproximationAuditReport:
    results: list[ShockPointResult] = []
    for spot_shock in policy.spot_shocks:
        for vol_shock in policy.vol_shocks:
            for elapsed_days in policy.elapsed_days:
                comparison = delta_gamma_vega_approximation(
                    policy.option,
                    spot_shock=spot_shock,
                    vol_shock=vol_shock,
                    elapsed_days=elapsed_days,
                )
                exact = comparison["exact_pnl"]
                approximate = comparison["delta_gamma_vega_theta_approximation"]
                error = abs(comparison["approximation_error"])
                allowed = policy.absolute_tolerance + policy.relative_tolerance * abs(exact)
                ratio = error / allowed
                results.append(
                    ShockPointResult(
                        spot_shock=spot_shock,
                        vol_shock=vol_shock,
                        elapsed_days=elapsed_days,
                        exact_pnl=round(exact, 10),
                        approximate_pnl=round(approximate, 10),
                        approximation_error=round(error, 10),
                        allowed_error=round(allowed, 10),
                        tolerance_ratio=round(ratio, 10),
                        passed=error <= allowed,
                    )
                )
    return ApproximationAuditReport(
        base_price=round(price(policy.option), 10),
        absolute_tolerance=policy.absolute_tolerance,
        relative_tolerance=policy.relative_tolerance,
        total_points=len(results),
        passed_points=sum(point.passed for point in results),
        maximum_absolute_error=round(
            max(point.approximation_error for point in results), 10
        ),
        maximum_tolerance_ratio=round(max(point.tolerance_ratio for point in results), 10),
        points=tuple(results),
    )


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate a local Greek P&L approximation against full repricing."
    )
    parser.add_argument("policy", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = audit_approximation(parse_policy(load_policy_json(args.policy))).as_dict()
    except (ApproximationAuditInputError, ValueError) as exc:
        report = {
            "ok": False,
            "total_points": 0,
            "passed_points": 0,
            "points": [],
            "issues": [{"code": "input.invalid", "message": str(exc)}],
        }
    if args.output is None:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _write_report(args.output, report)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
