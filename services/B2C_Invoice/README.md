# B2C Invoice — Walmart order sync (Lambda)

**Python 3.12** Lambda (no VPC): Walmart Marketplace API → PostgreSQL (`walmart_order_headers`, `walmart_order_lines`, `walmart_log`).

| Function | Trigger | Role |
|----------|---------|------|
| `walmartSync` | EventBridge every 15 min (`mode: updation`) | Incremental sync from `walmart_log` watermark |
| `walmartSync` | Manual invoke | Backfill: `mode: creation` + date range |
| `walmartDashboardApi` | HTTP API (`GET /v1/walmart/*`) | Read-only JSON for dashboard |

## Deploy

```bash
# From repo root
bash scripts/build_shared_layer.sh
cd services/B2C_Invoice
npm install

export DATABASE_URL='postgresql://...?sslmode=require'
export WM_BASIC_AUTH='Basic ...'

npx serverless deploy --stage prod --region ap-south-1
```

GitHub Actions: run **Deploy** workflow with service **`B2C_Invoice`**. Add repository secret **`WM_BASIC_AUTH`**.

## Manual invoke (backfill)

```json
{
  "mode": "creation",
  "created_start_date": "2026-06-01T08:00:00Z",
  "created_end_date": "2026-07-01T08:00:00Z"
}
```

## Local CLI

```bash
cd services/B2C_Invoice
# Optional: src/walmart/credentials_local.py (gitignored) for DATABASE_URL + WM_BASIC_AUTH
PYTHONPATH=src:../../layers/build/python python -m walmart.order_creation \
  --created-start-date 2026-06-01T08:00:00Z \
  --created-end-date 2026-07-01T08:00:00Z

PYTHONPATH=src:../../layers/build/python python -m walmart.order_updation

# SAP FG items: HANA → Postgres (local only; create table first)
# psql "$DATABASE_URL" -f ../../sql/sap_fg_items.sql
# export HANA_USE_SSM=1
# PYTHONPATH=src:../../layers/build/python python scripts/load_sap_fg_items.py
```

## Dashboard (RDS → API → browser)

Static report UI in `dashboard/` — **opens and loads data automatically** (API URL is in `app.js`).

| File | Purpose |
|------|---------|
| `index.html` | Layout + sidebar + report table |
| `styles.css` | Delivery-style green sidebar + status badges |
| `app.js` | Fetches API on page load (`API_BASE` at top) |

**Open locally:** use all three files in the same folder:

```text
services/B2C_Invoice/dashboard/index.html
```

Do not copy only `index.html` to Desktop — `app.js` and `styles.css` must sit beside it.

If the browser blocks `fetch` from `file://`, run:

```bash
cd services/B2C_Invoice/dashboard && python3 -m http.server 8080
```

Then open `http://localhost:8080`.

**Change API URL:** edit `API_BASE` in `dashboard/app.js` (e.g. prod stage).

| Endpoint | Description |
|----------|-------------|
| `GET /v1/walmart/stats` | Totals, status breakdown, last sync |
| `GET /v1/walmart/orders?limit=50&status=Shipped` | Recent order lines |
| `GET /v1/walmart/sync-log?limit=10` | Sync run history |

**S3 hosting (optional):** upload all of `dashboard/` to S3 (+ CloudFront for HTTPS).

**Auth:** API is open today — add API Gateway authorizer before sharing widely.

## Dashboard auth (Cognito)

The static dashboard uses **Cognito** (1 user) + **JWT authorizer** on all `/v1/walmart/*` routes.

**After deploy**, create the dashboard user:

```bash
cd services/B2C_Invoice
export STAGE=dev
export DASHBOARD_EMAIL='you@example.com'
export DASHBOARD_PASSWORD='YourSecurePass123'
bash scripts/create_walmart_dashboard_user.sh
```

Re-upload `dashboard/` to S3 after UI changes. Sign-in is required before any order data loads.

Public auth routes (no JWT): `GET /v1/auth/config`, `POST /v1/auth/login`, `POST /v1/auth/refresh`.

## Emergency: stop schedule

EventBridge → Rules → disable the `walmartSync` schedule rule.
