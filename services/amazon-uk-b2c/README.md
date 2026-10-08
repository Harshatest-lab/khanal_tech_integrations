# Amazon UK B2C — SP-API → SAP B1 (Lambda)

**Python 3.12** Lambdas: Amazon UK SP-API → PostgreSQL (`amazonuk.*` schema)
→ SAP B1 Service Layer AR Invoices + HANA FEFO batches. Ported from the
standalone pipeline at `~/amazon-uk` — business logic untouched, only the
entrypoint/credential layer adapted for Lambda. Structured like
`amazon-us-b2c`: one handler per job in `src/handlers.py`, workers in a
`src/amazon_uk/` package.

## Functions

| Function | Handler | Trigger | Role |
|----------|---------|---------|------|
| `amazonUkFetch` | `fetch_handler` | EventBridge cron `22:00 UTC` (03:30 IST), **enabled** | Incremental order fetch by `lastUpdatedAfter` watermark (window capped at 48 h) + trailing shipment report → `amazonuk.order_items` |
| `amazonUkPostInvoices` | `post_invoices_handler` | EventBridge cron `23:00 UTC` (04:30 IST), **enabled** | Post every Shipped, not-yet-posted order shipped on/after `AMZ_UK_POSTING_CUTOFF_DATE` to SAP B1 as an AR Invoice (failed orders are re-attempted every run) |
| `amazonUkFetchRefunds` | `fetch_refunds_handler` | EventBridge cron `23:30 UTC` (05:00 IST), **enabled** | Incremental RELEASED-refund fetch by `postedAfter` watermark (Finances API) → `amazonuk.refund_events` / `refund_items` |
| `amazonUkPostCreditNotes` | `post_credit_notes_handler` | EventBridge cron `00:00 UTC` (05:30 IST), **enabled** | Post every not-yet-posted RELEASED refund posted on/after `AMZ_UK_POSTING_CUTOFF_DATE` (same cutoff invoices use) to SAP B1 as an AR Credit Memo, one per refund transaction (failed refunds are re-attempted every run) |
| `amazonUkDashboardApi` | `dashboard_handler` | API Gateway HTTP API (512 MB / 30 s) | Cognito-protected **read-only** (GET) JSON API at `/api/*`. Static frontend is hosted separately on S3 (see Dashboard section) |

> **Was one function** (`amazonUkB2cDaily`, fetch + post in one 900 s budget).
> Split because the order fetch paces at ~180 s/page and the shipment report
> can poll ~10 min — a slow fetch could starve posting, and the dashboard's
> batch buttons hit API Gateway's 30 s cap. Fetch and post now each get their
> own 900 s.

### Runs on schedule only

Fetch and post are triggered **only** by their EventBridge cron rules — the
dashboard cannot start anything (its `/api` is GET-only; any other method
returns 405). Nothing needs seeding or clicking:

- **Fetch catches up by itself.** One run's `lastUpdatedAfter` window is
  capped at 48 h (`sync_amazonuk.MAX_WINDOW_HOURS`); a capped run moves the
  watermark to the window's end, so the first-ever run (7-day default) or a
  gap after downtime drains over consecutive daily runs instead of timing out.
  Each run bounds the work to fit the 900 s limit.
- **Post is gated by a required cutoff.** `AMZ_UK_POSTING_CUTOFF_DATE`
  (YYYY-MM-DD, no default — `serverless deploy` fails if it isn't exported)
  limits automatic posting to orders whose ship date (the invoice DocDate) is
  on/after it. A missing/invalid value stops the run before any SAP call and
  triggers the crash alert.
- **Failures self-heal.** Every post run re-attempts recorded failures
  (still inside the cutoff); a crash sends an SES alert and marks the
  invocation failed.

Break-glass (AWS credentials required — `serverless invoke -f … -d '…'` or
the Lambda console; not reachable from the dashboard):

```jsonc
// amazonUkFetch
{"mode": "backfill", "from_date": "2026-08-01", "to_date": "2026-08-31"}
// amazonUkPostInvoices
{"mode": "retry"}
{"amazon_order_id": "203-1234567-1234567"}     // one order; NOT limited by the cutoff
// amazonUkFetchRefunds
{"mode": "backfill", "from_date": "2026-08-01", "to_date": "2026-08-31"}
// amazonUkPostCreditNotes
{"mode": "retry"}
{"transaction_id": "..."}     // one refund transaction; NOT limited by the cutoff
```

Every handler goes through `_run()`: a crash sends an SES alert (secrets
scrubbed) and re-raises so the invocation shows as failed in CloudWatch.

## Dashboard

- **Static site on S3, same URL shape as the Walmart dashboard** —
  `AmazonUkDashboardSiteBucket` (plain S3 website hosting, defined in
  `serverless.yml`, public-read — inherent to S3's website-hosting feature,
  and the site only ever contains a Cognito client id + this API's URL,
  neither secret) at the deploy output `DashboardUrl`. `amazonUkDashboardApi`
  now serves **only** `/api/*` — no static files are packaged into it.
- Content isn't pushed by `serverless deploy` — sync it separately after
  every deploy or UI change:
  ```bash
  aws s3 sync services/amazon-uk-b2c/dashboard/ \
    s3://amazon-uk-b2c-dev-dashboard-site/ \
    --region ap-south-1 --exclude "serve.sh"
  ```
  (swap `dev` for the stage; the bucket name always follows
  `${service}-${stage}-dashboard-site`.)
- `dashboard/app.js` **hardcodes** `API_BASE` (this stage's API Gateway URL)
  and the Cognito `COGNITO_CLIENT_ID`/`COGNITO_REGION` at the top of the
  file — there's no `/config.js` generation anymore since the frontend
  isn't served by the Lambda. Update those constants (and re-run the sync
  above) if this is ever promoted to a different stage/account — the same
  known limitation the Walmart dashboard has with its own hardcoded
  `API_BASE`, just documented here instead of left as a surprise.
- `/api/*` routes are Cognito JWT-protected (own user pool, created by the
  stack; outputs give the pool/client ids). Create a user in the console
  (`cognito-idp` → set a permanent password) to log in.
- CORS: `provider.httpApi.cors` in `serverless.yml` scopes
  `Access-Control-Allow-Origin` to the dashboard bucket's own website origin
  (not `"*"`) — this API touches SAP/financial data.
- **Read-only.** No fetch/post/retry buttons and no POST routes. The
  orders list, refunds list, per-order/per-refund trace (including the SAP
  payload *preview*, which only reads HANA/stored SAP responses), SAP
  response, sync history and SKU map are all GET. `post_single_refund()`
  remains unused by the dashboard on purpose — posting only ever happens
  from `amazonUkPostCreditNotes`'s schedule.

## Layout

- `src/handlers.py` — the five Lambda entrypoints + `_run()` crash alerting.
- `src/amazon_uk/`
  - `sync_amazonuk.py` — `sync_incremental()` / `sync()` (order fetch orchestration).
  - `post_to_sap.py` — invoice posting orchestration (dedup → payload → SAP → record).
  - `sync_refunds.py` — `sync_incremental()` / `sync()` (refund fetch orchestration).
  - `post_credit_notes_to_sap.py` — credit-note posting orchestration, same
    dedup → payload → SAP → record shape as `post_to_sap.py`, keyed on
    `transaction_id` (not `amazon_order_id` — one order can get more than
    one RELEASED refund).
  - `fetch_refunds.py` — Finances API (`/finances/2024-06-19/transactions`)
    paging, RELEASED `Refund` transactions only.
  - `credit_note_payload.py` — builds the AR Credit Memo payload via SAP's
    "Copy From" (BaseType/BaseEntry/BaseLine against the original invoice).
  - `dashboard.py` — the read-only `/api` routes as `dispatch(method, path, qs)`.
  - `store.py` — RDS access (`connect()`, batch `upsert_orders()`/`upsert_refunds()`, watermark).
  - `sap_client.py` — SAP B1 Service Layer on **`khanal_shared.sap_sl`**
    (one fresh Login per run using `credentials.get_sap_sl_credentials()`;
    UDF PATCH flow preserved; duplicate guard fails loud; covers both
    Invoices and CreditNotes).
  - `hana_client.py` — FEFO + item-master on **`khanal_shared.hana.pool`**
    (SSM-backed via `HANA_USE_SSM`/`HANA_PROFILE`).
  - `alerts.py` — SES failure alerts + secret scrubbing (pulls current
    secret values from `credentials.py` / `khanal_shared.hana`, not raw env).
  - `credentials.py` — LWA / DB / SAP SL credentials, SSM by default
    (`AMZ_UK_LWA_USE_SSM`, `AMZ_UK_DB_USE_SSM`, `SAP_SL_USE_SSM`), cached
    in-process; falls back to plain env vars when the flag is off.
  - `config.py` — `config.get()` reads `os.environ` (non-secret config only
    now — e.g. `HANA_SCHEMA`, `AMZ_UK_POSTING_CUTOFF_DATE`).
  - `{amazon_auth,sp_api,fetch_orders,fetch_shipment_report,applog,invoice_payload}.py`
    — verbatim ports (imports adapted to the package).

## Deploy

Credentials live in SSM (SecureString), not exported plaintext env vars —
seed these once per environment (adjust the placeholder values):

```bash
aws ssm put-parameter --name /amazon-uk/lwa --type SecureString --value \
  '{"client_id":"...","client_secret":"...","refresh_token":"..."}'
aws ssm put-parameter --name /amazon-uk/db_config --type SecureString --value \
  '{"host":"...","port":5432,"dbname":"...","user":"...","password":"..."}'
aws ssm put-parameter --name /sap/uk/sl_config --type SecureString --value \
  '{"sap_b1_url":"...","companydb":"..."}'
aws ssm put-parameter --name /sap/uk/credentials --type SecureString --value \
  '{"username":"...","password":"..."}'
aws ssm put-parameter --name /sap/uk/hana_config --type SecureString --value \
  '{"host":"...","port":30015,"schema":"..."}'
aws ssm put-parameter --name /sap/uk/hana_credentials --type SecureString --value \
  '{"username":"...","password":"..."}'
```

```bash
# From repo root
psql "$DB_URL" -f sql/amazon_uk_b2c.sql      # once per environment
bash scripts/build_shared_layer.sh
cd services/amazon-uk-b2c
npm install

# REQUIRED — posting cutoff (deploy fails without it)
export AMZ_UK_POSTING_CUTOFF_DATE=YYYY-MM-DD
# strongly recommended — nobody triggers runs by hand, so crashes must
# reach someone. SES-verified addresses; alerts are skipped when unset.
export AMZ_UK_ALERT_ADMIN_EMAIL=... AMZ_UK_ALERT_FROM_EMAIL=...

npx serverless deploy --stage dev --region ap-south-1

# Dashboard static files aren't part of the Lambda package anymore — sync
# them to the new S3 bucket separately (see Dashboard section for details):
aws s3 sync dashboard/ s3://amazon-uk-b2c-dev-dashboard-site/ \
  --region ap-south-1 --exclude "serve.sh"
```

All four cron rules are **enabled** in `serverless.yml`, so they start firing
right after the deploy (order fetch 22:00 UTC, invoice post 23:00 UTC,
refund fetch 23:30 UTC, credit-note post 00:00 UTC). To pause one, set
`enabled: false` and redeploy.

To fall back to plaintext env vars instead of SSM for any one credential
(e.g. local override, a stage without SSM set up), set its `*_USE_SSM` flag
to `false` (`AMZ_UK_LWA_USE_SSM`, `AMZ_UK_DB_USE_SSM`, `SAP_SL_USE_SSM`,
`HANA_USE_SSM`) and export the corresponding plain vars — same as before
this migration.

## Tests

```bash
python -m pytest services/amazon-uk-b2c/tests -q     # needs: pytest, psycopg[binary], requests
```

Run per-service — not part of the root `pytest.ini` because `handlers` is
also a module name in `B2B_so/src`.

## Notes

- SAP session: one fresh Login per run — never touches the shared
  `/sap/inc*` SSM *session* parameters (Walmart / US pipelines). Only the
  SAP SL/HANA/LWA/DB *credentials* are SSM-backed (`/sap/uk/*`, `/amazon-uk/*`).
- Postgres driver is **psycopg 3** (what the shared layer ships), using
  `ClientCursor` (client-side binding) so the original psycopg2-era SQL
  behaves identically.
- Logs go to CloudWatch *and* `amazonuk.sync_log` in RDS (`applog.py`).
- Not built yet: a CloudWatch alarm for a scheduled run that silently
  never fires (crashes are covered by the SES alert + failed-invocation metric).
- Schema: `sql/amazon_uk_b2c.sql` (repo root) creates the `amazonuk` schema
  — `order_items`, `sku_item_map`, `sap_postings`, `fetch_log`, `sync_log`,
  `refund_events`, `refund_items`, `sap_credit_notes`. Run once per
  environment before the first invoke; idempotent.
- `AMZ_UK_POSTING_CUTOFF_DATE` gates both invoice posting (by ship date)
  and credit-note posting (by refund posted_date) — one operational knob,
  not two.
