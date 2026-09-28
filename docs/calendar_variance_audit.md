# Calendar total-variance admission audit

This audit rejects an implied-volatility surface when total implied variance
`w(T, k) = sigma(T, k)^2 T` decreases with expiry at a fixed forward-log-moneyness
node. Monotone total variance is a necessary calendar-arbitrage condition for a
Black-style surface and is useful before calibration, interpolation, scenario
generation, or risk aggregation.

## Evidence contract

The JSON artifact binds one fresh surface snapshot to an underlying and a
surface revision. Every quote supplies a governed `node_id`, expiry, strike,
forward, and implied volatility. The audit recomputes `log(strike / forward)`
and fails closed if quotes assigned to one node are not aligned within the
configured tolerance. It then sorts maturities and compares adjacent total
variances, independent of input order.

The implementation also rejects stale or future-dated snapshots, expired
quotes, duplicate node/expiry pairs, unknown fields, non-finite numbers,
undersized nodes, duplicate JSON keys, and resource-budget violations. Reports
contain only bounded node hashes and deterministic SHA-256 evidence; the raw
underlying identifier is not emitted. Exit codes are `0` for acceptance, `2`
for a valid artifact with policy breaches, and `3` for malformed evidence.

```bash
python -m optirisk.calendar_variance surface.json --output report.json
```

## Operational boundary

This is a necessary-condition gate, not a complete proof that a volatility
surface is arbitrage-free. It does not interpolate unmatched moneyness nodes,
validate bid/ask executability, detect butterfly arbitrage, infer forward or
discount curves, handle American exercise or discrete dividends, or verify the
quote producer. The node grid, curve inputs, freshness window, and numerical
tolerances must be governed upstream. Passing evidence does not establish
liquidity, model suitability, P&L, or safe live trading.

The next production increment is to derive forward-aligned nodes from
timestamped bid/ask quotes and signed curve snapshots, then combine this gate
with vertical-spread and convexity checks in one provenance-bound surface
admission decision.
