# Greek approximation validity envelope

Local Greeks are useful for fast P&L explain, but their Taylor approximation
breaks down as spot, volatility, or time shocks move away from the expansion
point. `optirisk.approximation_audit` turns that limitation into an executable
release gate by comparing delta/gamma/vega/theta P&L with full Black-Scholes
repricing over a prespecified shock grid.

Example policy:

```json
{
  "option": {
    "spot": 100.0,
    "strike": 100.0,
    "maturity_years": 0.5,
    "rate": 0.03,
    "volatility": 0.20,
    "option_type": "call",
    "dividend_yield": 0.01
  },
  "spot_shocks": [-0.02, 0.0, 0.02],
  "vol_shocks": [-0.01, 0.0, 0.01],
  "elapsed_days": [0, 1],
  "absolute_tolerance": 0.01,
  "relative_tolerance": 0.05
}
```

Run:

```bash
python -m optirisk.approximation_audit policy.json \
  --output artifacts/greek-envelope.json
```

Each point passes when:

```text
|approximate P&L - full-repricing P&L|
  <= absolute_tolerance + relative_tolerance * |full-repricing P&L|
```

The command exits `0` only when every point passes. Invalid policies or failed
shock points exit `2`. Reports include every exact/approximate P&L comparison,
the worst absolute error, and the worst tolerance ratio; output is written
atomically.

## Production safeguards

- every numeric input must be finite;
- option maturity is capped at 100 years, volatility at 1,000%, spot/strike at
  `1e12`, and rate/dividend yield at ±100% to prevent pathological arithmetic;
- spot shocks are bounded to ±50%;
- shocked volatility must stay positive;
- elapsed time must remain before expiry;
- shock axes must be non-empty and duplicate-free;
- the Cartesian grid is capped at 1,000 points;
- policy files are capped at 1 MB and duplicate JSON keys, non-finite constants,
  non-UTF-8 data, and symlinks are rejected.

## Scope and limitations

This validates the existing Black-Scholes local approximation; it is not a P&L
backtest and does not model discrete hedging, transaction costs, volatility-smile
dynamics, jumps, or path dependence. Tolerances must be chosen before reviewing
candidate results and should reflect the downstream use of the approximation.

The next increment should replay historical spot/volatility paths with discrete
delta hedging and transaction costs, then attribute residual P&L to model,
rebalancing, and execution effects.
