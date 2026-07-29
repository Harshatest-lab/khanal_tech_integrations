# Amazon US B2C — SP-API → SAP B1 (Lambda)

**Python 3.12** Lambdas (no VPC): Amazon SP-API → PostgreSQL staging →
SAP B1 Service Layer (INC entity, profile `inc_amzn` → `/sap/inc_amzn/*`
SSM params — own B1 session, so a 401 here never force-expires Walmart's
`/sap/inc/*` session, or vice versa; same physical SAP tenant, isolated
session cache only). HANA stays on the shared read-only `inc` profile.
~15-20k orders/month.

Business logic ported from the production-validated Frappe pipeline
(June 2026 reconciliation closed at 99.55% matched) — every rule below is
a verified production lesson, not a guess.

| Function | Trigger | Role |
|----------|---------|------|
| `amazonFetch` | hourly (`mode: incremental`) + daily (`mode: report`, 30-day MTR lookback) + manual backfill | Stage orders + lines; `ready` when Shipped + all SKUs mapped |
| `amazonFbaShipSync` | daily + manual | FBA shipments report → `actual_ship_date` (= SAP DocDate) |
| `amazonPostInvoices` | hourly window + daily `mode: retry` | Claim `ready` → dedup vs SAP → post AR Invoice |
| `amazonPostCreditNotes` | every 6 h (45-day window) | Finances refund events → AR Credit Notes (no inventory movement) |
| `amazonRecon` | daily (`mode: invariants`) + manual (`mode: mtr`) | Pipeline invariants + monthly MTR reconciliation |

All schedules ship **disabled** — enable after the TEST-SAP pilot passes.

## Non-negotiable business rules (do not "simplify" these away)

1. **Gross pricing** — UnitPrice = item_price/qty, freight = gross
   shipping. Promotions are never netted; Finance books promo separately.
2. **DocDate = actual_ship_date**, and posting REFUSES orders without a
   ship date (MTR indexes by ship date; purchase-date fallback puts
   invoices in the wrong month).
3. **SAP dedup before every post** by NumAtCard with
   `CancelStatus eq 'csNo'` (never `Cancelled`), and a failed lookup
   means DO NOT POST — never "not found".
4. **Line UDFs**: U_Order/U_OrderedOn/U_BuyerName/U_City/U_State/
   U_Country/U_PINCode only (U_State = USPS 2-letter code). The Service
   Layer silently drops unknown UDF names.
5. **Credit notes**: `WithoutInventoryMovement='tYES'`, no BatchNumbers,
   BaseType-13 linked, shipping + shipping tax reversed, and the stored
   invoice DocEntry is ownership-verified (NumAtCard) before linking.
6. **Posting cutoff** (`AMZ_POSTING_CUTOFF_DATE`): pre-cutoff revenue was
   manually booked by Finance — posting it again double-counts.
7. **SKU map**: SAP `OITM.U_Amz_SKU` with control-character stripping;
   ambiguous SKUs (multiple items) are never guessed — add an `override`
   row in `amazon_us_sku_map`.

## Deploy

```bash
# From repo root
bash scripts/build_shared_layer.sh          # includes psycopg + hdbcli + requests
cd services/amazon-us-b2c
npm install

export DATABASE_URL='postgresql://...?sslmode=require'
export AMZ_ALERT_ADMIN_EMAIL='you@khanalfoods.com'
export AMZ_ALERT_FROM_EMAIL='techadmin@khanalfoods.com'

npx serverless deploy --stage dev --region ap-south-1
```

One-time setup (human ops actions, never in git):

1. `psql "$DATABASE_URL" -f ../../sql/amazon_us_b2c.sql`
2. SSM SecureString `/amazon-us/lwa`:
   `{"client_id":"...","client_secret":"...","refresh_token":"..."}`
3. Create `/sap/inc_amzn/sl_config` and `/sap/inc_amzn/credentials`
   (SecureString, same shape as Walmart's `/sap/inc/*` — copy its values,
   this is the same physical SAP entity, only the session cache is
   separate). `/sap/inc_amzn/b1_session` does not need pre-creating; the
   first login writes it. HANA keeps reading the existing shared
   `/sap/inc/hana_config` + `/sap/inc/hana_credentials` (read-only, no
   session state, safe to share).
   **TEST phase**: point `/sap/inc_amzn/credentials` CompanyDB *and*
   `/sap/inc/hana_config` schema at the TEST DB **together** — split
   targets mean HANA reads and SAP writes hit different companies.
4. Verify SES identities for the alert addresses.

## Runbook

```bash
# Backfill a window (report path, chunked <=30 days automatically)
aws lambda invoke --function-name amazon-us-b2c-dev-amazonFetch \
  --payload '{"mode":"backfill","from_date":"2026-07-01","to_date":"2026-07-20"}' out.json

# Dry-run posting (computes + dedup-checks, never claims/posts/mutates)
aws lambda invoke --function-name amazon-us-b2c-dev-amazonPostInvoices \
  --payload '{"dry_run":true,"limit":50}' out.json

# Monthly MTR recon (upload the Custom Unified Transaction CSV to the
# recon bucket first)
aws lambda invoke --function-name amazon-us-b2c-dev-amazonRecon \
  --payload '{"mode":"mtr","s3_bucket":"amazon-us-b2c-recon-dev","s3_key":"mtr/2026-07.csv","period_from":"2026-07-01","period_to":"2026-07-31"}' out.json
```

Emergency stop: EventBridge → Rules → disable the schedule rules.

## Tests

```bash
cd services/amazon-us-b2c
python -m pytest tests/ -q     # needs: pytest, requests, psycopg
```

## Cutover from the Frappe pipeline

1. TEST SAP: single-order pilot, verify the document by hand in SAP.
2. Parallel dry-run against production for ~1 week (Frappe keeps
   posting; this service runs `dry_run: true` and the daily invariants).
3. Cutover: disable the Frappe scheduler first, then enable the
   EventBridge schedules here. Both sides share NumAtCard idempotency,
   so rollback is safe in either direction.
