# Finance Reconciliation Dashboard Section — Design

## Context

The amazon-us-b2c pipeline already has a monthly MTR (Marketplace Transaction Report)
reconciliation process, implemented in `recon.py:run_mtr_recon`. It's invoked manually
(Lambda `amazonRecon`, `mode: mtr`, with `s3_bucket`/`s3_key`/`period_from`/`period_to`
as input — someone has to have already uploaded the Amazon MTR CSV export to S3
before running it). It:

- Parses the MTR CSV, aggregating buyer-facing GROSS sales/refunds per order id.
- Pulls the SAP side (`OINV`/`ORIN` totals per order) from HANA.
- Compares to the cent and categorizes every order id into one bucket: `matched`,
  `missing_in_sap`, `amount_mismatch`, `refund_not_yet_posted`,
  `sap_not_in_mtr_timing`, `refunded_after_mtr_extract`, or `pre_cutoff_excluded`.
- Writes a detail CSV to the private `amazon-us-b2c-recon-{stage}` S3 bucket
  (key `recon/amazon-us-mtr-recon_{period_from}_{period_to}.csv`).
- Emails a summary via SES.
- Records a row in `amazon_us_sync_log` (`mode = "mtr_recon"`) whose `counts` JSON
  already contains everything needed for a summary view: `total_orders`, `matched`,
  `matched_pct`, `result_key`, and one count per category.

Today, the only way anyone (Finance included) sees results is that summary email
plus manually opening the CSV in S3. The amazon-us-b2c dashboard
(`services/amazon-us-b2c/dashboard/`) already exists (static HTML/JS/CSS, Cognito-
gated, no build step) with panels for Orders, Invoice failures, Credit notes, Sync
history, and Pipeline health. This adds one more panel to it.

Finance hasn't run a real MTR recon against this pipeline yet — it's still
pre-cutover — so there is no real historical data to show until we manually
generate one test run.

## Goals

- Finance (using the same dashboard login as ops — no new auth model) can see the
  history of MTR reconciliation runs: period, status, matched %, and the category
  breakdown, without needing the summary email or direct S3 access.
- Finance can download a given run's detail CSV directly from the dashboard.
- No change to how a recon run is *triggered* — that stays a manual Lambda invoke
  exactly as it works today. This is a read-only addition.

## Non-goals

- Not adding a way to trigger a new MTR recon run from the UI.
- Not adding a CSV upload flow for the MTR export — the file still has to already
  be sitting in S3 before anyone runs the recon.
- Not building row-level drill-down into individual order mismatches inside the
  dashboard — that stays in the downloadable CSV.
- Not adding a separate Cognito pool / access tier for Finance.

## Architecture

### Frontend (services/amazon-us-b2c/dashboard/)

One new panel, "Finance Reconciliation," added to the existing nav (same pattern as
the Pipeline health panel added earlier this session — new `<section>` in
`index.html`, new entry in `PANELS`/`PANEL_TITLES` in `app.js`).

**Data source:** the *existing* `GET /v1/amazon/sync-log?mode=mtr_recon&limit=N`
endpoint — no new listing query needed. Each row's `counts` field already has
everything required for the table.

**Rendering:** one row per past run —
`window_from → window_to | status | matched/total (pct%) | category chips | Download CSV button`.
Category chips: `matched` in green; `sap_not_in_mtr_timing`, `refunded_after_mtr_extract`,
`pre_cutoff_excluded` in muted/amber (expected, no action needed per `recon.py`'s own
documented categorization); `missing_in_sap`, `amount_mismatch`, `refund_not_yet_posted`
in red (need Finance's attention). Zero-count categories are omitted from the chip row
to avoid clutter.

**Empty state:** "No reconciliation runs yet" when the mode has no rows — expected
until Finance's first real month-end close post-cutover.

### Backend (services/amazon-us-b2c/src/)

One new route: `GET /v1/amazon/recon-download?key=<result_key>`.

- Added to `dashboard_handler` in `handlers.py`, gated by the same
  `dashboardCognitoJwt` authorizer as every other dashboard route (added in
  `serverless.yml`).
- Validates `key` starts with the literal prefix `recon/` (the only prefix
  `run_mtr_recon` ever writes to) — rejects anything else with 400, so this
  endpoint can never be used to fetch arbitrary objects out of the bucket.
- Calls `boto3.client("s3").generate_presigned_url("get_object", Params={"Bucket":
  constants... (AMZ_RECON_BUCKET env var), "Key": key}, ExpiresIn=300)` and returns
  `{"url": "..."}` as JSON. Frontend does `window.open(url)` to trigger the browser's
  own download.
- No new IAM needed: `provider.iam.role.statements` in `serverless.yml` is
  service-wide, and `amazonDashboardApi` already has `s3:GetObject` on
  `${self:custom.reconBucket}/*` from the existing MTR-recon IAM grant.

### Deploy note

This is the first change this session that needs a **real `serverless deploy`**,
not a code-only Lambda zip update — adding an HTTP API route + wiring its
authorizer is a CloudFormation-level change (API Gateway resource), not just a
Lambda code change. Every other fix this session was code-only precisely because
it didn't touch routes/infra; this one does.

## Data flow

1. Dashboard loads "Finance Reconciliation" panel → `GET /v1/amazon/sync-log?mode=mtr_recon&limit=20`.
2. Render table from `items[].counts` + `window_from`/`window_to`/`status`.
3. User clicks "Download CSV" on a row → `GET /v1/amazon/recon-download?key=<that row's counts.result_key>`.
4. Backend validates prefix, returns presigned URL.
5. Frontend opens the URL in a new tab — browser downloads the CSV directly from S3, dashboard/API never touches the file content.

## Error handling

- `recon-download` with a `key` not starting with `recon/` → `400 {"error": "invalid key"}`.
- `recon-download` with a `key` that doesn't exist in S3 → presigned URL still generates (S3 doesn't validate existence at signing time); the resulting download will 403/404 from S3 directly. Acceptable — this only happens if someone tampers with the key param, which the prefix check already constrains to the recon bucket's own layout.
- Sync-log fetch failure (network, auth) → same existing error-banner pattern already used by every other panel.

## Testing plan

1. Manually invoke `amazonRecon` (`mode: mtr`) against a small synthetic MTR CSV
   (a handful of order ids, some matched, some deliberately mismatched) uploaded to
   the recon bucket first, so there's one real `mtr_recon` sync-log row with a real
   result CSV to test against — not just an empty state.
2. Deploy the new `recon-download` route via `serverless deploy` (dev stage).
3. Run the dashboard locally (`serve.sh`), verify: panel loads real data, category
   chips render correctly (colors matching the categorization rules above), download
   button produces a working presigned URL that actually downloads the CSV.
4. Check browser console for errors, same verification bar as every other panel
   added this session.
5. Only after local verification passes do we commit + push + open a PR.

## Open questions / risks

- None outstanding — all three scope-defining questions (access model, detail
  level, trigger scope) were resolved in brainstorming before this spec was written.
