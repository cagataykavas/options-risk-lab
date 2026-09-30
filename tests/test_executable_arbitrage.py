from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from optirisk.black_scholes import EuropeanOption, price
from optirisk.executable_arbitrage import (
    ArtifactError,
    AuditPolicy,
    audit_snapshot,
    load_artifact,
)

NOW = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def quote(option_type: str, strike: float, bid: float, ask: float) -> dict[str, object]:
    return {
        "option_type": option_type,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "observed_at": "2026-09-30T23:59:30Z",
    }


@pytest.fixture
def clean_snapshot() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "snapshot_id": "surface-20260930T235930Z",
        "maturity_years": 1.0,
        "rate": 0.0,
        "dividend_yield": 0.0,
        "spot_bid": 99.95,
        "spot_ask": 100.05,
        "observed_at": "2026-09-30T23:59:30Z",
        "quotes": [
            quote("call", 90, 11.9, 12.1),
            quote("call", 100, 6.9, 7.1),
            quote("call", 110, 3.9, 4.1),
            quote("put", 90, 1.9, 2.1),
            quote("put", 100, 6.9, 7.1),
            quote("put", 110, 13.9, 14.1),
        ],
    }


def codes(report: dict[str, object]) -> list[str]:
    return [finding["code"] for finding in report["findings"]]  # type: ignore[index]


def test_accepts_executable_arbitrage_free_snapshot(clean_snapshot: dict[str, object]) -> None:
    report = audit_snapshot(clean_snapshot, evaluated_at=NOW)

    assert report["status"] == "accepted"
    assert report["summary"]["strategy_checks"] == 20  # type: ignore[index]
    assert report["findings"] == []


@pytest.mark.parametrize(
    ("index", "bid", "ask", "expected"),
    [
        (1, 12.2, 12.3, "EXECUTABLE_VERTICAL_MONOTONICITY"),
        (0, 18.0, 18.1, "EXECUTABLE_VERTICAL_SLOPE"),
        (3, 7.2, 7.3, "EXECUTABLE_VERTICAL_MONOTONICITY"),
        (5, 18.0, 18.1, "EXECUTABLE_VERTICAL_SLOPE"),
    ],
)
def test_detects_executable_vertical_violations(
    clean_snapshot: dict[str, object],
    index: int,
    bid: float,
    ask: float,
    expected: str,
) -> None:
    clean_snapshot["quotes"][index]["bid"] = bid  # type: ignore[index]
    clean_snapshot["quotes"][index]["ask"] = ask  # type: ignore[index]

    report = audit_snapshot(clean_snapshot, evaluated_at=NOW)

    assert report["status"] == "rejected"
    assert expected in codes(report)


@pytest.mark.parametrize("option_type", ["call", "put"])
def test_detects_executable_butterfly_credit(
    clean_snapshot: dict[str, object], option_type: str
) -> None:
    rows = [row for row in clean_snapshot["quotes"] if row["option_type"] == option_type]  # type: ignore[union-attr]
    rows[1]["bid"] = 9.0

    report = audit_snapshot(clean_snapshot, evaluated_at=NOW)

    matching = [
        finding
        for finding in report["findings"]
        if finding["code"] == "EXECUTABLE_BUTTERFLY_CONVEXITY"
        and finding["option_type"] == option_type
    ]
    assert matching[0]["strikes"] == [90.0, 100.0, 110.0]
    assert matching[0]["executable_profit"] > 0


def test_detects_call_rich_put_call_parity(clean_snapshot: dict[str, object]) -> None:
    clean_snapshot["quotes"][1]["bid"] = 9.0  # type: ignore[index]

    report = audit_snapshot(clean_snapshot, evaluated_at=NOW)

    assert "EXECUTABLE_PUT_CALL_PARITY_CALL_RICH" in codes(report)


def test_detects_put_rich_put_call_parity(clean_snapshot: dict[str, object]) -> None:
    clean_snapshot["quotes"][4]["bid"] = 9.0  # type: ignore[index]

    report = audit_snapshot(clean_snapshot, evaluated_at=NOW)

    assert "EXECUTABLE_PUT_CALL_PARITY_PUT_RICH" in codes(report)


def test_respects_profit_hurdle(clean_snapshot: dict[str, object]) -> None:
    clean_snapshot["quotes"][1]["bid"] = 12.105  # type: ignore[index]

    report = audit_snapshot(
        clean_snapshot,
        evaluated_at=NOW,
        policy=AuditPolicy(min_executable_profit=0.01),
    )

    assert "EXECUTABLE_VERTICAL_MONOTONICITY" not in codes(report)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda data: data.update(spot_bid=101.0), "CROSSED_SPOT_MARKET"),
        (lambda data: data["quotes"][0].update(bid=13.0), "CROSSED_OPTION_MARKET"),
        (lambda data: data["quotes"][0].update(bid=0.01, ask=2.0), "EXCESSIVE_RELATIVE_SPREAD"),
        (lambda data: data.update(observed_at="2026-09-30T23:55:00Z"), "STALE_SNAPSHOT"),
        (lambda data: data["quotes"][0].update(observed_at="2026-10-01T00:01:00Z"), "FUTURE_QUOTE"),
    ],
)
def test_rejects_market_quality_and_time_failures(
    clean_snapshot: dict[str, object], mutation: object, expected: str
) -> None:
    mutation(clean_snapshot)  # type: ignore[operator]

    assert expected in codes(audit_snapshot(clean_snapshot, evaluated_at=NOW))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(schema_version="2.0"),
        lambda data: data.update(snapshot_id="contains spaces"),
        lambda data: data.update(maturity_years=0),
        lambda data: data.update(rate=float("nan")),
        lambda data: data.update(observed_at="2026-10-01T00:00:00"),
        lambda data: data["quotes"].append(deepcopy(data["quotes"][0])),
        lambda data: data["quotes"][0].update(strike=-1),
        lambda data: data["quotes"][0].update(strike=1e16),
        lambda data: data["quotes"][0].update(bid=-1),
        lambda data: data.update(unexpected=True),
    ],
)
def test_malformed_artifacts_fail_closed(
    clean_snapshot: dict[str, object], mutation: object
) -> None:
    mutation(clean_snapshot)  # type: ignore[operator]

    with pytest.raises(ArtifactError):
        audit_snapshot(clean_snapshot, evaluated_at=NOW)


def test_rejects_non_datetime_evaluation_time(clean_snapshot: dict[str, object]) -> None:
    with pytest.raises(ArtifactError, match="must be a datetime"):
        audit_snapshot(clean_snapshot, evaluated_at="2026-10-01T00:00:00Z")  # type: ignore[arg-type]


def test_enforces_quote_and_strategy_budgets(clean_snapshot: dict[str, object]) -> None:
    with pytest.raises(ArtifactError, match="too many call quotes"):
        audit_snapshot(
            clean_snapshot,
            evaluated_at=NOW,
            policy=AuditPolicy(max_quotes_per_type=2),
        )

    with pytest.raises(ArtifactError, match="strategy-check budget"):
        audit_snapshot(
            clean_snapshot,
            evaluated_at=NOW,
            policy=AuditPolicy(max_strategy_checks=5),
        )


def test_report_is_deterministic_and_hides_raw_prices(clean_snapshot: dict[str, object]) -> None:
    first = audit_snapshot(clean_snapshot, evaluated_at=NOW)
    reordered = deepcopy(clean_snapshot)
    reordered["quotes"].reverse()  # type: ignore[union-attr]
    second = audit_snapshot(reordered, evaluated_at=NOW)

    assert first == second
    serialized = json.dumps(first, sort_keys=True)
    assert "11.9" not in serialized
    assert len(first["evidence_sha256"]) == 64  # type: ignore[arg-type]


def test_findings_are_bounded(clean_snapshot: dict[str, object]) -> None:
    for row in clean_snapshot["quotes"]:  # type: ignore[union-attr]
        row["bid"] = row["ask"] + 20

    report = audit_snapshot(
        clean_snapshot,
        evaluated_at=NOW,
        policy=AuditPolicy(max_reported_findings=3),
    )

    assert len(report["findings"]) == 3  # type: ignore[arg-type]
    assert report["summary"]["findings_truncated"] is True  # type: ignore[index]
    assert report["summary"]["finding_count"] > 3  # type: ignore[index]


def test_maximum_quote_budget_is_deterministic_and_bounded() -> None:
    quotes: list[dict[str, object]] = []
    for strike in range(60, 108):
        for option_type in ("call", "put"):
            fair = price(
                EuropeanOption(
                    spot=100.0,
                    strike=float(strike),
                    maturity_years=1.0,
                    rate=0.02,
                    volatility=0.25,
                    option_type=option_type,  # type: ignore[arg-type]
                )
            )
            quotes.append(quote(option_type, float(strike), max(0.0, fair - 0.005), fair + 0.005))
    payload = {
        "schema_version": "1.0",
        "snapshot_id": "bounded-48x2",
        "maturity_years": 1.0,
        "rate": 0.02,
        "dividend_yield": 0.0,
        "spot_bid": 99.995,
        "spot_ask": 100.005,
        "observed_at": "2026-09-30T23:59:30Z",
        "quotes": quotes,
    }
    policy = AuditPolicy(max_relative_spread=2.0)

    first = audit_snapshot(payload, evaluated_at=NOW, policy=policy)
    second = audit_snapshot(payload, evaluated_at=NOW, policy=policy)

    assert first == second
    assert first["status"] == "accepted"
    assert first["summary"]["strategy_checks"] == 39_200  # type: ignore[index]


def test_loader_rejects_duplicate_and_nonfinite_json(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"1.0","schema_version":"1.0"}')
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value":NaN}')

    with pytest.raises(ArtifactError, match="duplicate JSON field"):
        load_artifact(duplicate)
    with pytest.raises(ArtifactError, match="non-finite JSON"):
        load_artifact(nonfinite)


def test_loader_enforces_byte_budget(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_text(" " * 33)

    with pytest.raises(ArtifactError, match="byte budget"):
        load_artifact(path, AuditPolicy(max_input_bytes=32))


def test_cli_exit_codes_and_atomic_output(
    tmp_path: Path, clean_snapshot: dict[str, object]
) -> None:
    source = tmp_path / "snapshot.json"
    destination = tmp_path / "report.json"
    source.write_text(json.dumps(clean_snapshot))
    command = [
        sys.executable,
        "-m",
        "optirisk.executable_arbitrage",
        str(source),
        "--output",
        str(destination),
        "--evaluated-at",
        NOW.isoformat(),
    ]

    accepted = subprocess.run(command, check=False, capture_output=True, text=True)
    assert accepted.returncode == 0
    assert json.loads(destination.read_text())["status"] == "accepted"

    clean_snapshot["quotes"][1]["bid"] = 20.0  # type: ignore[index]
    source.write_text(json.dumps(clean_snapshot))
    rejected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert rejected.returncode == 2
    assert json.loads(destination.read_text())["status"] == "rejected"

    source.write_text("not json")
    malformed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert malformed.returncode == 3
    assert json.loads(malformed.stdout)["status"] == "malformed"
