from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from optirisk.calendar_variance import (
    SCHEMA_VERSION,
    ArtifactError,
    CalendarPolicy,
    audit_calendar_variance,
    load_artifact,
)

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)


def quote(
    node: str, expiry: str, strike: float, forward: float, volatility: float
) -> dict[str, object]:
    return {
        "node_id": node,
        "expiry": expiry,
        "strike": strike,
        "forward": forward,
        "implied_volatility": volatility,
    }


def artifact(quotes: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2026-09-28T14:59:00Z",
        "underlying_id": "portfolio-demo-index",
        "surface_revision": "vendor-a/2026-09-28T14:59:00Z",
        "quotes": quotes,
    }


def clean_quotes() -> list[dict[str, object]]:
    return [
        quote("atm", "2026-10-28T14:59:00Z", 100.0, 100.0, 0.20),
        quote("atm", "2026-12-28T14:59:00Z", 102.0, 102.0, 0.21),
        quote("put-10", "2026-10-28T14:59:00Z", 90.0, 100.0, 0.25),
        quote("put-10", "2026-12-28T14:59:00Z", 91.8, 102.0, 0.24),
    ]


class CalendarVarianceTests(unittest.TestCase):
    def test_accepts_increasing_total_variance(self) -> None:
        result = audit_calendar_variance(artifact(clean_quotes()), now=NOW)
        self.assertTrue(result.accepted)
        self.assertEqual(result.comparison_count, 2)
        self.assertEqual(result.breach_count, 0)

    def test_rejects_decreasing_total_variance(self) -> None:
        quotes = clean_quotes()
        quotes[1]["implied_volatility"] = 0.08
        result = audit_calendar_variance(artifact(quotes), now=NOW)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason_codes, ("TOTAL_VARIANCE_DECREASE",))
        self.assertEqual(result.breach_count, 1)
        self.assertGreater(result.max_variance_drop, 0)

    def test_compares_adjacent_expiries(self) -> None:
        quotes = clean_quotes()[:2]
        quotes.append(quote("atm", "2027-03-28T14:59:00Z", 104.0, 104.0, 0.22))
        result = audit_calendar_variance(artifact(quotes), now=NOW)
        self.assertEqual(result.comparison_count, 2)

    def test_input_order_does_not_change_evidence(self) -> None:
        forward = audit_calendar_variance(artifact(clean_quotes()), now=NOW)
        reverse = audit_calendar_variance(
            artifact(list(reversed(clean_quotes()))), now=NOW
        )
        self.assertEqual(forward.evidence_digest, reverse.evidence_digest)
        self.assertEqual(forward.to_dict(), reverse.to_dict())

    def test_report_does_not_expose_underlying_identifier(self) -> None:
        serialized = json.dumps(
            audit_calendar_variance(artifact(clean_quotes()), now=NOW).to_dict()
        )
        self.assertNotIn("portfolio-demo-index", serialized)

    def test_policy_changes_evidence_identity(self) -> None:
        base = audit_calendar_variance(artifact(clean_quotes()), now=NOW)
        changed = audit_calendar_variance(
            artifact(clean_quotes()),
            policy=CalendarPolicy(variance_tolerance=1e-8),
            now=NOW,
        )
        self.assertNotEqual(base.policy_digest, changed.policy_digest)
        self.assertNotEqual(base.evidence_digest, changed.evidence_digest)

    def test_breach_report_is_bounded(self) -> None:
        quotes = []
        for index in range(10):
            quotes.extend(
                [
                    quote(f"n-{index}", "2026-10-28T14:59:00Z", 100.0, 100.0, 0.8),
                    quote(f"n-{index}", "2026-12-28T14:59:00Z", 100.0, 100.0, 0.1),
                ]
            )
        result = audit_calendar_variance(
            artifact(quotes), policy=CalendarPolicy(max_reported_breaches=3), now=NOW
        )
        self.assertEqual(result.breach_count, 10)
        self.assertEqual(len(result.breaches), 3)

    def test_tolerance_allows_tiny_numerical_drop(self) -> None:
        quotes = clean_quotes()[:2]
        first = audit_calendar_variance(artifact(quotes), now=NOW)
        short_variance = 0.20**2 * (30 * 24 * 60 * 60) / (365.25 * 24 * 60 * 60)
        long_years = (91 * 24 * 60 * 60) / (365.25 * 24 * 60 * 60)
        quotes[1]["implied_volatility"] = math.sqrt(
            (short_variance - 5e-11) / long_years
        )
        result = audit_calendar_variance(artifact(quotes), now=NOW)
        self.assertTrue(first.accepted)
        self.assertTrue(result.accepted)

    def test_rejects_moneyness_drift_inside_claimed_node(self) -> None:
        quotes = clean_quotes()[:2]
        quotes[1]["strike"] = 103.0
        with self.assertRaisesRegex(ArtifactError, "moneyness mismatch"):
            audit_calendar_variance(artifact(quotes), now=NOW)

    def test_configurable_moneyness_tolerance(self) -> None:
        quotes = clean_quotes()[:2]
        quotes[1]["strike"] = 102.00005
        result = audit_calendar_variance(
            artifact(quotes), policy=CalendarPolicy(moneyness_tolerance=1e-5), now=NOW
        )
        self.assertTrue(result.accepted)

    def test_rejects_duplicate_node_expiry(self) -> None:
        quotes = clean_quotes()[:2]
        quotes.append(dict(quotes[0]))
        with self.assertRaisesRegex(ArtifactError, "duplicate"):
            audit_calendar_variance(artifact(quotes), now=NOW)

    def test_rejects_insufficient_maturities(self) -> None:
        with self.assertRaisesRegex(ArtifactError, "insufficient"):
            audit_calendar_variance(artifact(clean_quotes()[:1]), now=NOW)

    def test_rejects_stale_artifact(self) -> None:
        with self.assertRaisesRegex(ArtifactError, "stale"):
            audit_calendar_variance(
                artifact(clean_quotes()),
                policy=CalendarPolicy(max_quote_age_seconds=30),
                now=NOW,
            )

    def test_rejects_future_dated_artifact(self) -> None:
        value = artifact(clean_quotes())
        value["generated_at"] = "2026-09-28T15:10:00Z"
        with self.assertRaisesRegex(ArtifactError, "future-dated"):
            audit_calendar_variance(value, now=NOW)

    def test_rejects_expired_quote(self) -> None:
        quotes = clean_quotes()[:2]
        quotes[0]["expiry"] = "2026-09-28T14:58:00Z"
        with self.assertRaisesRegex(ArtifactError, "expiry"):
            audit_calendar_variance(artifact(quotes), now=NOW)

    def test_rejects_naive_timestamps(self) -> None:
        value = artifact(clean_quotes())
        value["generated_at"] = "2026-09-28T14:59:00"
        with self.assertRaisesRegex(ArtifactError, "timezone"):
            audit_calendar_variance(value, now=NOW)

    def test_rejects_non_finite_and_non_positive_numbers(self) -> None:
        for field, bad in (
            ("strike", math.nan),
            ("forward", 0),
            ("implied_volatility", math.inf),
        ):
            with self.subTest(field=field):
                quotes = clean_quotes()[:2]
                quotes[0][field] = bad
                with self.assertRaises(ArtifactError):
                    audit_calendar_variance(artifact(quotes), now=NOW)

    def test_rejects_derived_numeric_overflow(self) -> None:
        quotes = clean_quotes()[:2]
        quotes[0]["strike"] = 1e308
        quotes[0]["forward"] = 1e-308
        with self.assertRaisesRegex(ArtifactError, "moneyness ratio"):
            audit_calendar_variance(artifact(quotes), now=NOW)

        quotes = clean_quotes()[:2]
        quotes[0]["implied_volatility"] = 1e308
        with self.assertRaisesRegex(ArtifactError, "total implied variance"):
            audit_calendar_variance(artifact(quotes), now=NOW)

    def test_rejects_unknown_artifact_and_quote_fields(self) -> None:
        value = artifact(clean_quotes())
        value["unexpected"] = True
        with self.assertRaisesRegex(ArtifactError, "artifact fields"):
            audit_calendar_variance(value, now=NOW)
        value = artifact(clean_quotes())
        value["quotes"][0]["unexpected"] = True  # type: ignore[index]
        with self.assertRaisesRegex(ArtifactError, "quote fields"):
            audit_calendar_variance(value, now=NOW)

    def test_rejects_quote_and_node_budget_overflow(self) -> None:
        quotes = clean_quotes()
        with self.assertRaisesRegex(ArtifactError, "quote budget"):
            audit_calendar_variance(
                artifact(quotes), policy=CalendarPolicy(max_quotes=3), now=NOW
            )
        with self.assertRaisesRegex(ArtifactError, "node budget"):
            audit_calendar_variance(
                artifact(quotes), policy=CalendarPolicy(max_nodes=1), now=NOW
            )

    def test_rejects_invalid_policy(self) -> None:
        with self.assertRaises(ArtifactError):
            CalendarPolicy(min_maturities_per_node=1)
        with self.assertRaises(ArtifactError):
            CalendarPolicy(variance_tolerance=math.nan)

    def test_strict_loader_rejects_duplicate_keys_and_nan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            path.write_text('{"a":1,"a":2}', encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "duplicate JSON"):
                load_artifact(path)
            path.write_text('{"a":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "non-finite"):
                load_artifact(path)

    def test_loader_enforces_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ArtifactError, "byte budget"):
                load_artifact(path, max_bytes=1)

    def test_cli_returns_distinct_exit_codes_and_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            output_path = root / "report.json"
            generated_at = datetime.now(UTC)
            cli_quotes = [
                quote(
                    "atm",
                    (generated_at + timedelta(days=30)).isoformat(),
                    100.0,
                    100.0,
                    0.20,
                ),
                quote(
                    "atm",
                    (generated_at + timedelta(days=90)).isoformat(),
                    100.0,
                    100.0,
                    0.21,
                ),
            ]
            cli_artifact = artifact(cli_quotes)
            cli_artifact["generated_at"] = generated_at.isoformat()
            input_path.write_text(json.dumps(cli_artifact), encoding="utf-8")
            command = [
                sys.executable,
                "-m",
                "optirisk.calendar_variance",
                str(input_path),
                "--output",
                str(output_path),
            ]
            accepted = subprocess.run(
                command, check=False, capture_output=True, text=True
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertTrue(json.loads(output_path.read_text())["accepted"])

            values = [dict(value) for value in cli_quotes]
            values[1]["implied_volatility"] = 0.05
            cli_artifact["quotes"] = values
            input_path.write_text(json.dumps(cli_artifact), encoding="utf-8")
            rejected = subprocess.run(
                command, check=False, capture_output=True, text=True
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertFalse(json.loads(output_path.read_text())["accepted"])

            input_path.write_text("{}", encoding="utf-8")
            malformed = subprocess.run(
                command, check=False, capture_output=True, text=True
            )
            self.assertEqual(malformed.returncode, 3)


if __name__ == "__main__":
    unittest.main()
