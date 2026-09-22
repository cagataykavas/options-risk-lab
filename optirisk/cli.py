from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .arbitrage import ArbitragePolicy, OptionQuote, audit_quote_slice


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a European option quote slice for static arbitrage")
    parser.add_argument("input", type=Path, help="JSON quote-slice file")
    parser.add_argument("--require-clean", action="store_true", help="return exit code 2 when violations exist")
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        quotes = [OptionQuote(**quote) for quote in payload["quotes"]]
        policy = ArbitragePolicy(**payload.get("policy", {}))
        report = audit_quote_slice(
            quotes,
            spot=payload["spot"],
            expiry_years=payload["expiry_years"],
            rate=payload["rate"],
            dividend_yield=payload.get("dividend_yield", 0.0),
            policy=policy,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"accepted": False, "error": str(exc)}, sort_keys=True))
        return 1

    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    if args.require_clean and not report.accepted:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
