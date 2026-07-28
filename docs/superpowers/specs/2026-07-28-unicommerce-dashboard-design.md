# Unicommerce dashboard frontend design

**Status:** Approved, pending implementation.
**Repo:** `Khanal_tech_serverless`, `services/unicommerce-b2c/dashboard/` (new).
**Date:** 2026-07-28.

## Why

`unicommerceDashboardApi` (HTTP API + Cognito user pool) has been deployed
and live-verified on `dev` since 2026-07-24, but only by invoking the
Lambda directly with simulated HTTP-API events — bypassing the Cognito
authorizer entirely. No real frontend exists, and no real login has ever
been tested. `kiosk-frontend` (the original repoint target) is being
retired, not reused (2026-07-28 decision, see `docs/migration/STATUS.md`)
— so this is a new build, not a repoint.

## Architecture

`services/unicommerce-b2c/dashboard/` — plain static HTML/CSS/vanilla JS,
no framework, no build step, structurally cloned from the existing house
pattern at `services/amazon-us-b2c/dashboard/` (`index.html` + `app.js` +
`styles.css` + `serve.sh`, ~1450 lines total there). Runs locally via
`python3 -m http.server` (see `serve.sh`) — an internal ops tool, not a
hosted site; no S3/CloudFront needed.

- **Auth**: Cognito `USER_PASSWORD_AUTH` called directly from `app.js`
  against the already-deployed pool — `ap-south-1_h7aHtfcWw`, client
  `30aoa8aoru4dp31fhlrv80cpu2` (both confirmed live via `serverless info`
  + `describe-stacks` on the `dev` stack, 2026-07-28). No backend login
  proxy, matching amazon's pattern exactly.
- **API**: calls go straight to `https://uuid78uhog.execute-api.ap-south-1.amazonaws.com`
  with the Cognito ID token as `Authorization: Bearer <token>`.
- **Config**: `API_BASE` and `COGNITO_CLIENT_ID`/pool ID are hardcoded
  constants at the top of `app.js` (matching amazon's file — no env/build
  step to inject them differently). Swapping to `prod` later is a
  two-constant edit, no rebuild, per the existing "base-URL + auth swap"
  plan.

## Components (panels)

Five panels + a login screen, matching `amazon-us-b2c`'s dashboard shape
1:1 against what `unicommerceDashboardApi` actually exposes:

1. **Orders** (landing panel) — stat chips at the top (from `GET /stats`:
   counts by `pipeline_status`, `channel_name`, CN status, last sync per
   mode), a filterable/sortable/paginated table (`GET /orders` —
   `pipeline_status`, `channel_name`, `uniware_id` search, order-date
   range, sort), and a bulk **"Retry failed invoices"** button
   (`POST /retry-invoices`, no params → bulk mode, `limit=50` default).
   Matches amazon's exact retry UX: `window.confirm()` before firing,
   `window.alert()` with the raw JSON result after — no per-row retry,
   even though the API supports single-order retry via `uniware_id`
   (amazon's dashboard doesn't expose that either; not adding scope here).
2. **Returns / Credit notes** — table from `GET /returns` (`status`
   filter), bulk **"Retry failed credit notes"** button
   (`POST /retry-cn`, bulk mode).
3. **Sync history** — read-only table from `GET /sync-log` (`mode`
   filter) — mode, status, started/finished, window, counts, error.
4. **Recon / mismatches** — table from `GET /recon-gaps` (`min_diff`
   filter) — `uniware_id`, channel, SAP DocEntry, tax expected/actual/
   diff, unit-price expected/actual/diff, **and `ar_invoice_mismatch_reason`**
   (see backend change below), updated-at.
5. **Order detail modal** — triggered from clicking an Orders-table row,
   `GET /order-detail?uniware_id=...` — header, line items, linked
   credit notes.

## Backend change bundled with this build

`dashboard_queries.fetch_recon_gaps()` (`services/unicommerce-b2c/src/unicommerce/dashboard_queries.py`)
currently selects the numeric tax/unit-price diffs but not
`ar_invoice_mismatch_reason` — a column `Recon` already writes
(`services/unicommerce-b2c/src/unicommerce/recon.py`, see
`sql/unicommerce_b2c.sql:68`) with the human-readable line-level
diagnosis. Without it, the Recon panel can only show numbers, not why.
One-line addition to the existing `SELECT`; no new endpoint, no schema
change (column already exists).

## Data flow

Same client-side shape as amazon's dashboard: nav links toggle which
`<section class="panel">` is visible (`data-panel` attribute), each
panel has a `load()` function that fires its `GET` on panel activation
and on its Refresh button, table bodies are re-rendered from the JSON
response. No routing beyond that (no URL hash sync, matching amazon).

## Error handling

Shared `#error-banner` div shown on any fetch failure (matches amazon's
`showError()` pattern). Confirmed in amazon's `app.js`: both `apiGet` and
`apiPost` check `res.status === 401` inline and call
`showLoginScreen("Session expired — sign in again.")` — port this
exactly, same message text.

## Auth prerequisite

The Cognito pool has **zero users** as of 2026-07-28 (confirmed via
`aws cognito-idp list-users`) — nobody has tested a real login yet. A
user will be created via `admin-create-user` +
`admin-set-user-password` for **`yogesha@khanalfoods.com`** before this
is considered done.

## Testing plan

Manual, against real `dev` data (no sandbox/mocks — same discipline as
the rest of this migration):

- Serve locally (`./serve.sh`), log in with the real Cognito user,
  click through all 5 panels, confirm each renders real `dev`-stack data
  (staged orders/returns/sync-log/recon-gaps already exist from earlier
  live verification).
- Order-detail modal: open from a real order row, confirm header/lines/
  credit-notes render.
- **Retry buttons are NOT clicked for real during this pass** — they are
  live writes against production SAP/UC if there's queued work in
  `dev`'s DB (there is, from earlier sessions' live-verification
  testing). Wiring + confirm-dialog + error-path get verified by
  inspection and by the same manual Lambda-invoke method already used to
  verify these endpoints (2026-07-24/27), not by clicking the real
  button against live data as part of frontend QA.

## Scope decisions (confirmed, don't re-litigate)

- Full panel-set parity with `amazon-us-b2c`'s dashboard in this first
  pass — not a minimal single-panel smoke test.
- No "Health" panel — `unicommerceDashboardApi` has no equivalent
  endpoint; not inventing one for parity's sake.
- Bulk-only retry actions, no per-row retry — matches amazon's actual
  dashboard, not just the API's capability.
- ~~Static local-serve deployment (no S3/CloudFront)~~ — superseded
  2026-07-28: hosted on S3 + CloudFront (OAC, private bucket) at the
  user's request for a shareable URL. Bucket
  `unicommerce-b2c-prod-dashboard-site`, distribution defined in
  `services/unicommerce-b2c/serverless.yml`. `./serve.sh` still works
  for local dev; deploys to the CDN are a manual
  `aws s3 sync services/unicommerce-b2c/dashboard/ s3://unicommerce-b2c-prod-dashboard-site/`
  after any frontend change (no CI step wired up for this yet).
