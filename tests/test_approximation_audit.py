from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from optirisk.approximation_audit import (
    ApproximationAuditInputError,
    audit_approximation,
    load_policy_json,
    main,
    parse_policy,
)


def _policy(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "option": {
            "spot": 100.0,
            "strike": 100.0,
            "maturity_years": 0.5,
            "rate": 0.03,
            "volatility": 0.20,
            "option_type": "call",
            "dividend_yield": 0.01,
        },
        "spot_shocks": [-0.01, 0.0, 0.01],
        "vol_shocks": [-0.005, 0.0, 0.005],
        "elapsed_days": [0, 1],
        "absolute_tolerance": 0.01,
        "relative_tolerance": 0.05,
    }
    payload.update(overrides)
    return payload


def test_small_shock_envelope_passes() -> None:
    report = audit_approximation(parse_policy(_policy()))

    assert report.ok
    assert report.total_points == 18
    assert report.passed_points == 18
    assert report.maximum_tolerance_ratio <= 1.0


def test_strict_envelope_detects_large_shock_error() -> None:
    report = audit_approximation(
        parse_policy(
            _policy(
                spot_shocks=[-0.30, 0.30],
                vol_shocks=[-0.10, 0.10],
                elapsed_days=[30],
                absolute_tolerance=0.0001,
                relative_tolerance=0.001,
            )
        )
    )

    assert not report.ok
    assert report.passed_points < report.total_points
    assert report.maximum_tolerance_ratio > 1.0


def test_rejects_shock_that_makes_volatility_nonpositive() -> None:
    with pytest.raises(ApproximationAuditInputError, match="volatility strictly positive"):
        parse_policy(_policy(vol_shocks=[-0.20]))


def test_rejects_elapsed_time_at_or_after_expiry() -> None:
    with pytest.raises(ApproximationAuditInputError, match="before option expiry"):
        parse_policy(_policy(elapsed_days=[183]))


def test_rejects_pathological_contract_domain() -> None:
    option = dict(_policy()["option"])
    option["rate"] = -2.0

    with pytest.raises(ApproximationAuditInputError, match="option.rate must be between"):
        parse_policy(_policy(option=option))


def test_rejects_oversized_grid() -> None:
    spot = [index / 1000 for index in range(-10, 11)]
    vol = [index / 1000 for index in range(-10, 11)]
    days = list(range(3))

    with pytest.raises(ApproximationAuditInputError, match="maximum is 1000"):
        parse_policy(_policy(spot_shocks=spot, vol_shocks=vol, elapsed_days=days))


def test_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text('{"spot_shocks":[],"spot_shocks":[]}', encoding="utf-8")

    with pytest.raises(ApproximationAuditInputError, match="duplicate JSON key"):
        load_policy_json(path)


def test_loader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_policy()), encoding="utf-8")
    link = tmp_path / "policy.json"
    os.symlink(target, link)

    with pytest.raises(ApproximationAuditInputError, match="non-symlink"):
        load_policy_json(link)


def test_cli_writes_failure_report_and_returns_two(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    report_path = tmp_path / "report.json"
    policy_path.write_text(
        json.dumps(
            _policy(
                spot_shocks=[-0.30, 0.30],
                vol_shocks=[0.10],
                elapsed_days=[30],
                absolute_tolerance=0.0001,
                relative_tolerance=0.001,
            )
        ),
        encoding="utf-8",
    )

    exit_code = main([str(policy_path), "--output", str(report_path)])

    assert exit_code == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert not report["ok"]
    assert report["passed_points"] < report["total_points"]


def test_cli_writes_passing_report_and_returns_zero(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    report_path = tmp_path / "report.json"
    policy_path.write_text(json.dumps(_policy()), encoding="utf-8")

    exit_code = main([str(policy_path), "--output", str(report_path)])

    assert exit_code == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"]
    assert report["total_points"] == 18
