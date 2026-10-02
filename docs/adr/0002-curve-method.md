# 2. Curve method: spot anchor plus seasonal historical shape, M0–M4

Date: 2026-10-02 · Status: accepted

## Context
EIA publishes current daily spot prices for WTI and Brent, but stopped publishing NYMEX futures on 2024-04-05. Futures history covers WTI contracts 1–4 only, so real data supports a curve shape about four months out. EIA has no Brent futures.

## Options
1. Historical curves only: real data, but frozen at 2024.
2. **Spot anchor plus estimated shape, M0–M4.**
3. Extend to M12 with seasonal extrapolation.
4. A stochastic model (for example Schwartz mean reversion).

## Decision
Option 2. For a product with spot price `S` on as-of date `d` (calendar month `m`):

- `M0 = S`
- `Mk = S × exp(s[k, m])` for k = 1..4, where `s[k, m]` is the median of `ln(Fk / S)` over all dates in calendar month `m` within the estimation window.
- Estimation window: 2014-01-01 to 2024-04-05. Exclude non-positive prices: WTI settled negative on 2020-04-20, where the log is undefined. Glue/PySpark estimates `s[k, m]` and writes it as a versioned Gold parameter table.
- Brent borrows WTI's `s[k, m]` and is labelled `shape_source = WTI`.
- `Brent−WTI` is derived tenor by tenor from the two outright curves.
- Every curve point carries `method_version`.

## Consequences
- The method is simple to explain and test; parameters can be inspected and reproduced.
- The curves are a model, not market quotes, and the UI says so.
- Because Brent borrows WTI's shape, the Brent−WTI curve is nearly flat across tenors.
- Tenors beyond M4 and the stochastic model are left as later exercises.
