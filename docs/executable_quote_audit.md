# Executable option-quote arbitrage audit

Mid-price no-arbitrage checks are useful for surface calibration, but a
mid-price violation is not necessarily tradable. This audit asks the narrower
production question: **can every required leg cross the supplied bid/ask
spread and still lock in a policy-sized positive credit?**

The dependency-free runner evaluates one European-option expiry slice. It
checks:

- crossed and excessively wide markets;
- quote and snapshot freshness, including future-skew limits;
- call and put vertical monotonicity at executable prices;
- discounted vertical-spread slope bounds after reserving the maximum terminal
  liability;
- convexity through long-wing/short-body butterflies on irregular strikes;
- put-call parity using executable call, put and spot sides plus rate and
  dividend discounting.

It rejects malformed evidence before evaluation, including duplicate JSON
keys, non-finite values, timezone-naive timestamps, duplicate strikes,
unexpected fields, unsafe discount exponents and resource-budget violations.
The deterministic report contains policy and snapshot SHA-256 identities,
bounded reason codes and strike locations, but not raw bid/ask prices.

## Input contract

```json
{
  "schema_version": "1.0",
  "snapshot_id": "surface-20260930T235930Z",
  "maturity_years": 1.0,
  "rate": 0.0,
  "dividend_yield": 0.0,
  "spot_bid": 99.95,
  "spot_ask": 100.05,
  "observed_at": "2026-09-30T23:59:30Z",
  "quotes": [
    {
      "option_type": "call",
      "strike": 100.0,
      "bid": 6.9,
      "ask": 7.1,
      "observed_at": "2026-09-30T23:59:30Z"
    }
  ]
}
```

Run the audit with an explicit evaluation time for reproducible evidence:

```bash
python -m optirisk.executable_arbitrage snapshot.json \
  --evaluated-at 2026-10-01T00:00:00Z \
  --output quote-audit.json
```

Exit `0` means accepted, `2` means valid evidence rejected by policy, and `3`
means malformed or unevaluable input. Output-file replacement is atomic.

## Trust boundary and limitations

The snapshot must contain synchronized, firm and actually accessible quotes.
The audit does not prove that the quotes were executable, that sufficient size
was available, or that all legs could fill simultaneously. It omits fees,
taxes, borrow/short-sale constraints, stock-financing spreads, discrete
dividends, exercise/assignment risk, settlement conventions and latency between
venues. The continuous dividend-yield approximation is unsuitable when a
material discrete dividend is expected. American options require separate
early-exercise bounds.

The reported credit is per option-price unit before contract multipliers and
implementation costs. `min_executable_profit`, freshness and relative-spread
limits therefore need venue- and product-specific calibration. Passing this
gate does not validate implied volatilities or establish absence of calendar
arbitrage across expiries.

## Next production step

Bind each quote to venue, size, currency, settlement calendar and a signed
collector receipt; then apply fee, borrow and fill-probability haircuts before
promoting a detected opportunity to an execution alert.
