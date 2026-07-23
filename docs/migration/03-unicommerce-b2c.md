# Phase 1 — Unicommerce B2C → Lambda

## Source (Frappe, verified)

`khanal_tech_integrations/unicommerce/`:

- `workers/fetch.py` — `fetch_orders`, `fetch_returns`
- `workers/invoice.py` — `post_invoices`
- `workers/credit_note.py` — `post_credit_notes`
- `workers/grn.py` — `sync_grns`
- `workers/inventory.py` — `sync_inventory`
- `validators/reconcile.py` — `run_reconciliation_refresh`
- `client/unicommerce.py`, `client/sap.py`, `mappers/sap_payload.py`,
  `mappers/tax.py`, `validators/batches.py`, `validators/pre_check.py`
- Doctypes: `unicommerce_orders`, `unicommerce_returns`,
  `unicommerce_sap_update_log`, `unicommerce_settings`,
  `unicommerce_alert_recipient`
- Frontend already decoupled: `kiosk-frontend/src/admin/pages/UnicommerceDashboard.vue`
  + `kiosk-frontend/src/admin/components/unicommerce/*` (ActivityLog,
  FailuresTable, KpiCard, OpsPanel, AlertRecipientsCard, Banner,
  PrimaryActionCard, SectionHeader) — this is a Vue SPA hitting a REST
  API today; it does not need a rewrite, only a new API to point at.

Current cadence (from `hooks.py`, 30-min offsets so jobs don't collide):
fetch orders `2,32 * * * *`, post invoices `7,37`, fetch returns `12,42`,
post credit notes `17,47`, sync GRNs `24,54`, sync inventory `27,57`,
reconciliation refresh `29,59`.

## Architecture decision

This is the **easiest** phase — it's a set of stateless scheduled workers
with a well-defined external API (Unicommerce) and a well-defined
downstream (SAP Service Layer), no user-facing writes, no permission
model to reimplement. It follows the exact shape of `amazon-us-b2c`
almost line for line:

- New stack: `services/unicommerce-b2c/`
- One Lambda function per worker (`fetchOrders`, `fetchReturns`,
  `postInvoices`, `postCreditNotes`, `syncGrns`, `syncInventory`,
  `reconRefresh`), each on its own `schedule` event at the same cadence
  as today, **deployed with `enabled: false` initially** — same
  dual-run-safe pattern as Phase 0.5 taught us the hard way.
- One `unicommerceDashboardApi` function behind HTTP API + a new
  Cognito user pool (`unicommerce-b2c-<stage>-dashboard`), serving the
  same shape of endpoints `amazon-us-b2c` does
  (`/v1/unicommerce/stats`, `/orders`, `/returns`, `/sync-log`,
  `/retry-invoices`, `/recon-download` etc.) — `kiosk-frontend`'s
  Unicommerce components get repointed at this instead of Frappe's REST
  API, no rebuild needed, just a base-URL + auth swap.
- Reuse `khanal_shared`'s SAP Service Layer client and session-caching
  pattern (SSM-cached `b1_session`, same `/sap/inc/*` parameter family
  Amazon US and Walmart already share) — **do not** write a second SAP
  client. If Unicommerce needs a different SAP company/profile than
  `inc`, add a new SSM parameter family (e.g. `/sap/uc/*`) rather than
  branching logic inside the shared client.
- Postgres tables replace `unicommerce_orders`,
  `unicommerce_sap_update_log`, `unicommerce_returns` — new schema
  `unicommerce` in the shared RDS instance (`core`/`fulfillment`/
  `integrations` schema convention from `docs/DATABASE.md` — add
  `unicommerce.orders`, `unicommerce.returns`,
  `unicommerce.sap_update_log`, or fold into the generic
  `integrations.integration_jobs` / `integrations.external_entity_map`
  tables already documented there if the shape fits — check before
  inventing new tables).
- `unicommerce_settings`, `unicommerce_alert_recipient` become SSM
  parameters + an env var list, same as `AMZ_ALERT_ADMIN_EMAIL` in
  `amazon-us-b2c`.

## Infrastructure changes

- No VPC (RDS already public+SG-gated).
- IAM: per-parameter `ssm:GetParameter`/`PutParameter` scoped to
  `/sap/inc/*` and any new `/uc/*` params, `ses:SendEmail` for alerts,
  `s3:GetObject/PutObject` on a new `unicommerce-b2c-recon-<stage>`
  bucket if reconciliation exports files (mirrors `amazon-us-b2c`'s
  `ReconBucket`).
- CI: add `unicommerce-b2c` to the `service` input choices in
  `.github/workflows/deploy.yml`, plus any new secrets
  (`UC_API_KEY`/whatever Unicommerce's auth scheme requires — check
  `unicommerce/client/unicommerce.py` for the exact auth mechanism before
  assuming API-key vs OAuth).

## Cutover runbook

1. Build `services/unicommerce-b2c` with all schedules `enabled: false`,
   deploy to a `dev` stage, point at **test/sandbox Unicommerce
   credentials and a non-prod SAP company** if one exists (same mistake
   to avoid as Phase 0.5 — never point a freshly-deployed, untested stack
   at prod SAP with schedules on).
2. Manually invoke each function once (`serverless invoke -f fetchOrders`
   etc.) against sandbox data, verify output shape matches what the
   Frappe worker currently produces (same order fields, same SAP payload
   shape via `mappers/sap_payload.py`'s logic ported over).
3. Deploy to prod stage, **schedules still `enabled: false`**, prod SSM
   params set.
4. Enable **read-only** functions first (`fetchOrders`, `fetchReturns`)
   with schedules on, Frappe's fetch crons **left running in parallel**
   — compare row-for-row in Postgres vs what Frappe's
   `unicommerce_orders` doctype shows for the same window. This is safe
   because two fetch paths reading from Unicommerce don't conflict with
   each other or with SAP.
5. Once fetch matches for a few days, cut over the **write** paths
   (`postInvoices`, `postCreditNotes`, `syncGrns`, `syncInventory`,
   `reconRefresh`) one at a time — disable the corresponding Frappe cron
   entry immediately before enabling the matching Lambda schedule, same
   back-to-back discipline as Phase 0.5.
6. Repoint `kiosk-frontend`'s Unicommerce dashboard at the new API +
   Cognito pool.
7. After a stable verification window (your call, no deadline), delete
   the Frappe `unicommerce/` module and its doctypes.

## Risks

- SAP payload shape drift between the Frappe mapper and a reimplemented
  Lambda mapper — mitigate by porting `mappers/sap_payload.py` and
  `mappers/tax.py` logic as directly as possible rather than
  re-deriving it, and by diffing actual SAP payloads in a dry-run mode
  before enabling writes.
- Two systems fetching Unicommerce orders simultaneously could hit rate
  limits — check Unicommerce's API rate limit before running Frappe +
  Lambda fetch in parallel during step 4.

## Cost estimate (incremental, `ap-south-1`)

| Item | Estimate | Notes |
|---|---|---|
| Lambda (7 scheduled functions, 30-min cadence, seconds-scale duration) | $0–2/mo | Well inside the 1M req + 400,000 GB-s free tier at this volume |
| Dashboard Lambda + HTTP API | <$1/mo | Internal staff usage, low thousands of requests/mo, HTTP API is $1/1M requests |
| Cognito (new pool) | $0/mo | Essentials tier, free to 10,000 MAU |
| S3 recon bucket | <$1/mo | Small reconciliation exports |
| SES alerts | <$1/mo | $0.10/1,000 emails, failure-only volume |
| Postgres schema addition | $0 incremental | Uses the existing shared RDS instance; revisit only if storage/IOPS growth forces an instance-class bump |

**Total: roughly $2–5/month incremental**, assuming current order volume
(the existing `amazon-us-b2c` comments cite 15–20k orders/month as the
comparable scale for Amazon US; Unicommerce volume should be checked
against this before assuming it holds).
