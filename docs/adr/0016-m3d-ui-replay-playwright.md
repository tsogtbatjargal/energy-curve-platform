# 16. M3d: local UI, replay CLI and browser tests

Date: 2026-10-03 · Status: accepted

## Context
M3d adds the browser UI over the M3b/M3c API (ADR-0013, ADR-0014, ADR-0015):
- four tabs: Curves, History, Alerts, Health;
- a mobile layout;
- a replay CLI that shows live updates;
- Playwright tests.

ADR-0011 limits what the UI, screenshots and CI artefacts may show.

Verified on 2026-10-03:
- **w2ui 2.0.0** (npm registry):
  - it is the `latest` dist-tag, published 2023-04-26 and unchanged since;
  - licence MIT, no dependencies;
  - tarball integrity `sha512-oMvLitt2jMholqCpX0mDZ0b70ITjlLpfV56gswk7/cD2oPmlibV+DqcizESSs3+t5VJHiG0Xlcb23e4AoCN1Qw==` (checked against the downloaded file);
  - the ES-module build has no inline event handlers and no `eval`/`new Function`, but it does set inline `style` attributes and inserts `<style>` elements.
- **Playwright 1.63.0** (locked) installs Chromium 153 (headless shell build 1243).
- **`actions/upload-artifact`** latest release is v7.0.1, commit `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`.

## Decisions

### w2ui, vendored
- **What is vendored.** `w2ui-2.0.es6.min.js` and `w2ui-2.0.min.css` from the verified tarball, under `src/energy_curves/api/static/vendor/w2ui-2.0.0/`.
- **Licence.** The package ships no licence file, so the MIT text with the copyright named in its `package.json` sits alongside.
- **Integrity.** A test pins each vendored file's SHA-256, so any change to vendored code is deliberate.
- **No CDN:** the UI works offline and loads nothing third-party at runtime.
- **Release age.** 2.0.0 is three and a half years old with no later release. It is acceptable for a local UI with no untrusted input rendered as HTML (see *Rendering*). Revisit before any hosted deployment (M6).

### Serving and browser security
- **Same app.** The API app serves `GET /` (the page) and `/static/` (the UI files), so the page and the API share one origin. The Host/Origin guard and the absence of CORS (ADR-0014) cover the UI too.
- **Headers on every response:**
  - `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`
- **Why `'unsafe-inline'` for styles only.** w2ui needs it, and inline styles cannot run code. Scripts stay `'self'`-only. The browser tests fail on any CSP violation in the console.
- **Rendering.** Data reaches the page only as JSON and is written with `textContent` or escaped through `w2utils.encodeTags` before w2ui renders it.

### The page
- **Always visible:**
  - the banner "Modelled estimate, not market quotes";
  - a **SYNTHETIC DATA** badge whenever the dataset source is synthetic (ADR-0011);
  - the dataset version, and a live/reconnecting indicator.
- **Stale-data banner.** It appears when the newest curve point is more than 4 days old (a weekend is 2–3 days). It shows the as-of date and the age. The API computes the age per request (ADR-0014).

| Tab | Shows |
|---|---|
| **Curves** | Per curve, the latest available point of each position, with its own as-of date, age, status and estimate type; a CSV export link |
| **History** | Filters (curve, position, start, end) drive a table and a line chart of one position over time; gaps break the line; CSV export of the same selection |
| **Alerts** | Rules (create with curve, position and threshold; delete); fired alerts, updated live |
| **Health** | Published versions, failed and quarantined attempts, outbox counts and dead events |

**The History chart** is a small, hand-written SVG, so no chart library is vendored. It has an accessible name and the table as its text alternative.

**Mobile.** Below 700 px wide the tabs stay on one row, controls stack, and wide tables scroll inside their panel, never the page.

### Live updates in the page
- **Dataset updates.** `EventSource('/api/events')`: on `dataset_updated`, the page re-fetches the header and the open tab.
- **Alerts.** `EventSource('/api/alerts/events')`:
  - on `alert_fired`, the page adds the alert, de-duplicated by event id, and shows a notice;
  - on `alerts_reset`, it re-fetches `GET /api/alerts`.
- **First load.** The page loads `GET /api/alerts` first, then opens the stream with `?after=<cursor>`. After that, the browser's own reconnect sends `Last-Event-ID`, so alerts fired while it was disconnected arrive on reconnect (ADR-0015).

### Replay CLI
`energy-curves replay --store DIR --start DATE --end DATE [--warmup-days N] [--interval S] [--override SERIES=DATE:PRICE]` steps through business days of the **synthetic** source. Each day:
1. ingest that day into the store, which publishes a version with curves;
2. `db-import` it, publishing `dataset_updated` after the commit;
3. run one alert consumer pass;
4. wait `interval` seconds.

The first step publishes `--warmup-days` of history at once. `--override` injects a price, so a demo, or a test, can make a rule cross on a known day.

**Safety.** Replay uses only the synthetic source, so it never calls EIA. It refuses:
- a store that holds any non-synthetic published version;
- a database whose market holds non-synthetic data.

It never touches the real `data/` store unless that store is itself synthetic.

### Browser tests (Playwright, marker `browser`)
- **Setup.** Each test runs against a real uvicorn server in a thread, with a fresh Postgres database and a synthetic store, so nothing real reaches a screenshot or trace (ADR-0011).
- **Coverage:**
  - each tab;
  - the banner and badge;
  - the History filters and chart;
  - CSV export with its disclaimer;
  - the stale banner, on and off;
  - creating a rule and seeing its alert fire live during a replay step;
  - reconnecting after a missed alert: the server is stopped, an alert fires, the server restarts, and the page shows the alert without a reload;
  - a mobile-viewport smoke test with no page-level horizontal scroll;
  - no console errors, including CSP violations, in any of them.
- **CI.** The `python` job installs Playwright's pinned Chromium and runs the browser tests. Traces are kept on failure and uploaded with `actions/upload-artifact` (pinned to `043fb46…`). The `glue-compat` job excludes the `browser` marker: its purpose is the Glue runtime, not the UI.

## Consequences
- The UI adds no runtime network dependency and no new Python dependency (Playwright was already a dev dependency).
- CI downloads Chromium from Playwright's CDN in the `python` job, which is the only new external fetch.
- w2ui updates are manual: re-verify the tarball integrity, replace the files, update the pinned hashes.
