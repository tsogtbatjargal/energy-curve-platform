# 11. Third-party price data rights

Date: 2026-10-03 · Status: accepted

## Context
EIA data is generally public domain. But EIA's reuse policy excludes "information resources contributed or licensed by private individuals, companies, or organizations". The series this project uses come from third parties:

| Series | Provider named by EIA |
| --- | --- |
| WTI Cushing spot (RWTC), Brent spot (RBRTE) | Refinitiv, an LSEG business |
| WTI futures contracts 1–4 (RCLC1–RCLC4) | NYMEX (CME Group) |

No explicit redistribution grant was found for either source (checked 2026-10-03: EIA copyright page, spot and futures table definitions). EIA stopped publishing futures after 2024-04-05 and gives no reason.

## Decision
Until rights are confirmed, treat these series as **not redistributable**:
- The public repo contains no real price values. Test fixtures keep EIA's response structure with synthetic values, labelled synthetic.
- Real data is fetched with the user's own API key into a git-ignored local cache. Hashes and row counts of that cache may be committed; prices may not.
- Whether derived shape parameters (48 medians of log ratios) can be committed is an open question for the owner. Until it is decided they are generated locally.
- The M6 public snapshot needs its own decision (PLAN.md R3).

## Consequences
- A clean checkout runs the offline demo on synthetic data. The real-data golden test runs only where the local cache exists.
- The project can be shown publicly without republishing licensed market data.
