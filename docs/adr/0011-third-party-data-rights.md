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
- **Derived parameters** (decided 2026-10-03): parameters estimated from real data stay local (`data/shape/`, git-ignored). The repository and every public output use parameters estimated from the synthetic source only. Each parameter set records its `origin`, and a run refuses parameters whose origin differs from the data it ingests.
- The M6 public snapshot needs its own decision (PLAN.md R3). **Decided 2026-10-10: see the addendum.**

## Consequences
- A clean checkout runs the offline demo on synthetic data. The real-data golden test runs only where the local cache exists.
- The project can be shown publicly without republishing licensed market data.

## Addendum (2026-10-10): R3 decided, public outputs are synthetic only
**Decision (user).** No public output shows or derives from real EIA prices: not the GitHub Pages snapshot, not screenshots, not the video, not the README. Real data stays in the git-ignored `data/`, for local use only. Rights are not confirmed and are not pursued; this decision does not depend on them.

**What it means for M6.**
- The snapshot is built from the synthetic source (`ECP_SOURCE=synthetic`), the same generator that backs the tests, the offline demo and the cloud batch. Its curves, history, alerts and pipeline-health views are all labelled synthetic, as the API already does (`synthetic` flag and disclaimer on every data response).
- Shape parameters in anything public carry `origin: "synthetic"` (the rule since 2026-10-03). Parameters estimated from real data are not shown, even though they are derived values.
- The demo-day stack (M6) runs on synthetic data too; screenshots and the video are recorded from it or from the local synthetic demo, never from a session that has the real cache loaded.
- Real-data checks (`ECP_REAL_DATA_DIR=data uv run pytest -m realdata`) stay local and opt-in.

**Proposed enforcement (not built yet; each is a test or a gate, written before the M6 code it guards).**
1. **The snapshot build refuses a non-synthetic source.** The build reads the dataset manifest and fails unless every `source` is `synthetic` and the pointer's origin is synthetic; a test feeds it a real-looking manifest and expects the refusal.
2. **A snapshot content scan.** A test over the built snapshot (and any committed HTML, JSON, CSV or image metadata) fails on a real-source marker: a `source` other than `synthetic`, or an `x-synthetic: false` page, or a series value that equals one in a small committed list of real-price fingerprints (hashes only, never prices).
3. **The Pages workflow reads only the snapshot artifact**, in a job with no AWS credentials and no `data/` checkout; a workflow test pins this, as `tests/test_ci_batch_plan.py` does for the plan job.
4. **Screenshots and video:** a pre-commit check refuses image or video files under `docs/` unless they are listed in a manifest that names their origin (`synthetic`); the demo script starts the app with `ECP_SOURCE=synthetic` and the API's synthetic banner visible.
5. **README and docs:** the existing gitleaks and sanitization scans stay; a test fails on a price-like value next to a real series ID in tracked Markdown, unless it is the documented synthetic example (for example `61.25`).
6. **The cloud already complies:** the batch stack, the Glue job and the planned Redshift load run on the seeded synthetic generator only (ADR-0020, ADR-0023).

**What stays open.** Whether rights are ever confirmed, and what a future real-data public view would need, is out of scope; changing this decision is a new addendum and a change to the enforcement above.
