# Options Risk Lab

Dependency-free European option pricing, analytic and finite-difference Greeks,
portfolio scenarios, implied volatility, and a machine-readable static-arbitrage
gate for market quote ingestion.

## Static-arbitrage gate

`audit_quote_slice` validates paired call/put quotes for one expiry before they
reach calibration, implied-volatility, or risk pipelines. It checks:

- discounted lower and upper price bounds;
- put-call parity;
- call/put monotonicity and vertical-spread slope bounds; and
- butterfly convexity, including irregular strike spacing.

Every violation contains a stable reason code, option side, affected strikes,
observed value, and policy limit. Invalid or insufficient evidence fails closed.

```python
from optirisk import OptionQuote, audit_quote_slice

report = audit_quote_slice(
    [
        OptionQuote(90, 12.20, 0.860075),
        OptionQuote(100, 6.37, 4.881194),
        OptionQuote(110, 2.67, 11.032313),
    ],
    spot=100,
    expiry_years=0.5,
    rate=0.03,
)
print(report.to_dict())
```

The CLI accepts the same market inputs plus `quotes` in JSON. `--require-clean`
returns `0` for an accepted slice, `2` for policy violations, and `1` for
malformed evidence or configuration.

```bash
optirisk-audit quote-slice.json --require-clean
```

## Development

```bash
python -m pip install pytest ruff build
ruff check .
ruff format --check .
python -m pytest -q
python -m build
```

## Trust boundary and limitations

The gate assumes all quotes share the supplied spot, expiry, rate, dividend
yield, currency, settlement convention, and European exercise style. It does
not infer bid/ask executability, transaction costs, discrete dividends, early
exercise, stale timestamps, cross-expiry calendar arbitrage, or a full
volatility-surface guarantee. Production ingestion should validate those fields,
apply the gate to executable bid/ask combinations, and retain its JSON evidence.
