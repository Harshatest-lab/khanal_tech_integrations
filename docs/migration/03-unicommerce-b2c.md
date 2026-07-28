# Phase 1 — Unicommerce B2C → Lambda

## Source (Frappe, verified)

`khanal_tech_integrations/unicommerce/`:

Confirmed by reading the actual code (not just the file tree) on 2026-07-23,
during the Unicommerce fix pass (`Unicommerce_Fixes_Yogesha_23072026`) —
this is **six distinct flows**, not one generic "sync" job:

1. **Fetch** — orders + returns from Unicommerce (`workers/fetch.py`:
   `fetch_orders`, `fetch_returns`)
2. **Post AR Invoices** to SAP for fetched orders (`workers/invoice.py`:
   `post_invoices` — the heaviest one: atomic ready→posting claims,
   tri-state batch-managed caches, FEFO allocation, a reservation ledger
   to stop concurrent orders over-allocating the same batch, series/FY
   fallback logic)
3. **Post Credit Notes** to SAP for returns (`workers/credit_note.py`:
   `post_credit_notes`)
4. **Create + approve Purchase Orders in Unicommerce** — reverse
   direction, SAP Inventory Transfer → UC PO (`workers/grn.py`: `push_po`)
5. **Create GRNs in Unicommerce** after PO approval — completes the
   receipt cycle for #4 (`grn.py` / `po_grn.py`: `_push_grn_rounds`,
   `MAX_GRN_ROUNDS`)
6. **Push inventory adjustments to Unicommerce** — when SAP moves stock
   internally (EC-FG→EC-B2C), UC's shelf stock gets decremented to match
   (`workers/inventory.py`: `sync_inventory` / `_sync_one_transfer`)

Plus two things that ride on top of all six, not separate data flows:

- **Reconciliation/audit** — SAP vs UC vs expected-value matching
  (`validators/reconcile.py`, `fetch.py`'s `audit_staged_order_completeness`
  / `audit_staged_business_coverage`)
- **Ops Center** — retry, force-post, activity monitoring, failure alerts
  (`api/ops.py`) — this is the layer the (unmerged)
  `Unicommerce_Dashboard_Fix_Yogesha_03072026` and
  `Unicommerce_OrderHarmony_Yogesha_21072026` branches were rebuilding on
  the Frappe side; it becomes the Lambda dashboard API here instead.

Supporting code: `client/unicommerce.py`, `client/sap.py`,
`mappers/sap_payload.py`, `mappers/tax.py`, `validators/batches.py`,
`validators/pre_check.py`, `geo.py` (state/tax-bucket resolution),
`health.py`, `returns.py`.

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
- **Seven functions** (matches `amazon-us-b2c`'s ~6-function shape for a
  similarly-scoped integration — one function per flow above, not one
  per doctype):

  | Function | Frappe source | Trigger |
  |---|---|---|
  | `unicommerceFetch` | `fetch_orders` + `fetch_returns` (one handler, `input.mode` like `amazonFetch`'s `incremental`/`report`) | schedule, 30-min offset |
  | `unicommercePostInvoices` | `post_invoices` | schedule, 30-min offset |
  | `unicommercePostCreditNotes` | `post_credit_notes` | schedule, 30-min offset |
  | `unicommercePoGrn` | `push_po` + `_push_grn_rounds` (one handler, `input.mode`) | schedule, 30-min offset |
  | `unicommerceInventorySync` | `sync_inventory` | schedule, 30-min offset |
  | `unicommerceRecon` | `run_reconciliation_refresh` + the two audit functions | schedule, daily |
  | `unicommerceDashboardApi` | `api/ops.py` (Ops Center) | HTTP API, Cognito-gated |

  All scheduled functions **deployed with `enabled: false` initially** —
  same dual-run-safe pattern as Phase 0.5 taught us the hard way.
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

## Concurrency safety — carry forward from the Frappe fix pass

The 2026-07-23 Unicommerce fix pass (see
[`Unicommerce_Fixes_Yogesha_23072026`](https://github.com/Khanal-Tech/khanal_tech_integrations/tree/Unicommerce_Fixes_Yogesha_23072026)
in the Frappe repo) closed three double-submission races that exist
**because the Frappe code lets a manual "retry now" click and a scheduled
tick both act on the same row with no lock in between**. The Lambda
rewrite must not reintroduce these — the same three claim points need a
direct equivalent:

1. **Order posting** — `_claim_ready_order`'s pattern
   (`UPDATE ... SET status='posting' WHERE status='ready'`, check
   `rowcount`) translates directly to Postgres: `UPDATE unicommerce.orders
   SET status='posting' WHERE id=$1 AND status='ready' RETURNING id` —
   zero rows back means another invocation already owns it.
2. **PO creation** — same technique on `unicommerce.po_status`, sentinel
   value before calling UC's `purchaseOrder/create`.
3. **Inventory sync** — same technique on the inventory-transfer row's
   sync status before calling UC's `inventory/adjust/bulk`.

This matters more in Lambda than it did in Frappe: `unicommercePostInvoices`
running on a fixed schedule **and** being manually re-invoked (e.g. from
the Ops Center dashboard's "retry" button, once it exists) is the exact
same race shape, and concurrent Lambda invocations are if anything *more*
likely than concurrent Frappe background jobs, not less.

## Infrastructure changes

- No VPC (RDS already public+SG-gated).
- **Confirmed 2026-07-23**: Unicommerce is India B2C, hitting the same
  physical SAP Service Layer host `delivery-api` already talks to
  (`103.69.202.108:50000`, self-signed cert, `CN=INHYDMSRDVSA135` —
  verified directly via `openssl s_client`). So this is `delivery-api`'s
  entity family, **not** `/sap/inc/*` (the US entity
  `amazon-us-b2c`/`B2C_Invoice` use). No longer an open question.
- **Test isolation (2026-07-24)**: `delivery-api` already runs this
  entity **live in production** at the default `/sap/*` SSM param
  family, so `unicommerce-b2c` (still fully unverified) does NOT point
  at `/sap/*` — it uses its own profile, `SAP_SL_PROFILE`/`HANA_PROFILE
  = india_test` -> `/sap/india_test/*`, a confirmed-existing test
  company DB in the same SAP B1 instance. Separate SSM param family +
  session cache, so nothing here can ever touch delivery-api's live
  session. Cut over to the real India entity at go-live by removing the
  profile override in `serverless.yml` (falls back to default `/sap/*`)
  — never repoint `/sap/india_test/*`'s values to the prod company DB.
- **TLS**: this SAP host's cert is self-signed (confirmed above), so
  every Unicommerce Lambda function needs `SAP_SL_VERIFY_SSL:
  ${env:SAP_SL_VERIFY_SSL, 'true'}` in `serverless.yml` (copy from
  `delivery-api`'s) **and** the GitHub Actions `SAP_SL_VERIFY_SSL` secret
  set to `false` for this service's deploy — same as `delivery-api`
  already requires. `khanal_shared.sap_sl.http` already implements this
  toggle; nothing new to write, just wire the same env/secret through.
- IAM: per-parameter `ssm:GetParameter`/`PutParameter` scoped to the
  `/sap/india_test/*` family (not `/sap/*` — see test isolation above),
  plus a **new** SSM family
  for Unicommerce's own API credentials (`/unicommerce/api_credentials` —
  today these live in the `Unicommerce Settings` doctype, not SAP's own
  secrets), `ses:SendEmail` for alerts, `s3:GetObject/PutObject` on a new
  `unicommerce-b2c-recon-<stage>` bucket if reconciliation exports files
  (mirrors `amazon-us-b2c`'s `ReconBucket`).
- **Don't repeat the CI/deploy drift this repo just had fixed** (see
  `layers/build` untracking + CI/deploy service-list sync, 2026-07-23):
  add `unicommerce-b2c` to **both** `.github/workflows/ci.yml`'s package
  steps **and** `.github/workflows/deploy.yml`'s `service` input
  description **in the same PR that adds the service**, not after.
- Reuse the existing shared layer (`layers/build`, built by
  `scripts/build_shared_layer.sh`) as-is — Unicommerce's UC REST client
  only needs `requests`, already in the layer for `amazon-us-b2c`'s
  SP-API client. No new layer dependency needed unless a UC-specific
  library shows up during the port.

## Cutover runbook

**Updated 2026-07-28**: `InventorySync` (SAP outward transfer → UC stock
REMOVE) was built, live-tested, and deliberately deleted — UC already
decrements its own stock the moment an order ships, so a manual REMOVE
double-acted on the same event. It is not part of this runbook. The real
6-flow build is `Fetch`, `PostInvoices`, `PostCreditNotes`, `PoGrn`,
`Recon`, `DashboardApi` — see `2026-07-27-pogrn-inventorysync-design.md`
for the full removal rationale and `docs/migration/STATUS.md` for current
build/deploy status.

1. Build `services/unicommerce-b2c` with all schedules `enabled: false`,
   deploy to a `dev` stage, point at **test/sandbox Unicommerce
   credentials and a non-prod SAP company** if one exists (same mistake
   to avoid as Phase 0.5 — never point a freshly-deployed, untested stack
   at prod SAP with schedules on). **Done** — deployed to `dev` against
   the `india_test` SAP company.
2. Manually invoke each function once (`serverless invoke -f fetchOrders`
   etc.) against sandbox data, verify output shape matches what the
   Frappe worker currently produces (same order fields, same SAP payload
   shape via `mappers/sap_payload.py`'s logic ported over). **Done** —
   all 6 flows live-verified against `dev` + `india_test`, including
   real writes for `PoGrn` (PO8975).
3. Deploy to prod stage, **schedules still `enabled: false`**, prod SSM
   params set. **Not started** — prod SSM (`/unicommerce/api_credentials`,
   `/unicommerce/settings`, default `/sap/*`) is still empty.
4. Enable **read-only** functions first (`fetchOrders`, `fetchReturns`)
   with schedules on, Frappe's fetch crons **left running in parallel**
   — compare row-for-row in Postgres vs what Frappe's
   `unicommerce_orders` doctype shows for the same window. This is safe
   because two fetch paths reading from Unicommerce don't conflict with
   each other or with SAP.
5. Once fetch matches for a few days, cut over the **write** paths
   (`unicommercePostInvoices`, `unicommercePostCreditNotes`,
   `unicommercePoGrn`, `unicommerceRecon`) one at a time — disable the
   corresponding Frappe cron entry immediately before enabling the
   matching Lambda schedule, same back-to-back discipline as Phase 0.5.
   Suggested order: **post invoices** first (highest business value,
   most heavily reworked in the Frappe fix pass so its edge cases are
   freshest), **post credit notes** next (depends on a posted invoice
   already existing), **PoGrn** next (independent of the invoice/CN
   path — SAP-transfer-triggered, its own claim/state machine), **recon**
   last (reconciles what the other three just posted, so it has nothing
   to check until they're live).
6. Build a new dashboard against `unicommerceDashboardApi` (HTTP API +
   Cognito pool, already live on `dev`) — `kiosk-frontend` will not be
   repointed; it's being retired, not reused, per the 2026-07-28
   decision in `docs/migration/STATUS.md`.
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
- ~~Wrong SAP company DB if the `/sap/*` vs `/sap/inc/*` entity isn't
  confirmed before deploy~~ — resolved 2026-07-23, confirmed `/sap/*`
  (see Infrastructure changes above).
- Dropping the atomic-claim pattern during the port (see Concurrency
  safety above) silently reintroduces the exact three races the Frappe
  fix pass just closed — write a test for each claim point specifically,
  don't assume "porting the logic" automatically ports the locking.

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
