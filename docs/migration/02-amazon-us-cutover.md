# Phase 0.5 — Finish the Amazon US cutover

This isn't a new migration — `services/amazon-us-b2c` already exists in
this repo, fully built (fetch, invoice posting, credit notes, FBA
shipment sync, settlement sync, health check, reconciliation, dashboard
API). It's just **not live**: it was deployed recently with test-DB
credentials, and every scheduled trigger in `serverless.yml` is
`enabled: false`. Meanwhile Frappe's own `amazon_us.workers.*` crons are
still the ones running in prod against SAP — how many orders they've
posted isn't tracked anywhere accessible right now.

This is flagged as its own phase, ahead of Unicommerce, because it's a
**loaded gun sitting in the repo**: if someone flips the Lambda schedules
to `enabled: true` without first swapping credentials and disabling the
Frappe crons, both systems post invoices to the same SAP company DB for
the same orders — double-posted AR invoices, not a hypothetical.

## Architecture (unchanged — already built)

`services/amazon-us-b2c` — SP-API → SAP AR Invoices/Credit Notes,
Cognito-gated dashboard API, SES alerts, S3 recon bucket, SSM-scoped IAM,
no VPC. This matches the standard already documented in
[00-OVERVIEW.md](00-OVERVIEW.md). Nothing architectural needs to change —
this phase is a **credentials-and-cutover exercise**, not a build.

## What needs to happen, in this exact order

1. **Inventory current state.** In the AWS console for the account/stage
   this was deployed to: confirm exactly which SSM parameters
   (`/sap/inc/sl_config`, `/sap/inc/credentials`, `/sap/inc/hana_config`,
   `/sap/inc/hana_credentials`, `/amazon-us/lwa`) are set to test values
   vs prod values right now. Confirm `DATABASE_URL` for this stage too.
2. **Confirm Frappe's `amazon_us` crons' actual on/off state in prod** —
   don't assume from `hooks.py` alone; check the Frappe scheduler log for
   the last N days to see if `fba_shipments`, `settlement`, or
   `health_check` actually ran and whether they posted anything.
3. **Reconcile for double-processing risk.** Before flipping anything,
   check SAP for duplicate AR invoices/credit notes for the same Amazon
   order IDs — if the Lambda side has run against a **test DB** only, this
   should be a non-issue, but confirm before proceeding rather than after.
4. **Swap SSM parameters for this stage to prod values** (same parameter
   names, new values — `ssm:PutParameter`, requires the deploying
   principal to have write access, which is broader than the Lambda's own
   runtime IAM role).
5. **Disable the Frappe `amazon_us` cron entries** in `hooks.py` (same
   mechanical process as Phase 0) — this is the actual cutover moment.
   Do this **immediately before** step 6, not hours earlier, to minimize
   the window where neither side is posting.
6. **Flip `enabled: true`** on the relevant schedules in
   `services/amazon-us-b2c/serverless.yml` and redeploy.
7. **Watch for one full daily cycle**: CloudWatch logs for each function,
   SES failure alerts, and a manual SAP check that invoice counts/amounts
   look sane against what the Amazon Seller Central order volume would
   predict.
8. Only after a clean cycle (or several, your call given no deadline):
   delete the Frappe `amazon_us` module code, following the same
   dual-run-then-delete pattern as every later phase.

## Risk / rollback

- **Primary risk:** double-posting during the cutover window (steps 5–6).
  Mitigated by doing them back-to-back rather than on separate days.
- **Rollback:** re-enable the Frappe cron entries, set the Lambda
  schedules back to `enabled: false`. SSM parameters can stay on prod
  values (harmless if the Lambda side isn't triggering) or be reverted —
  your call based on whether you trust the Frappe side more mid-incident.
- No schema/data migration is involved in this phase, which is why it's
  lower-risk than Phases 1–3 despite being first: it's flipping switches
  on infrastructure that already exists, not building anything new.

## Cost impact

Effectively $0 incremental — the Lambda stack is already deployed and
billed at whatever its idle/scheduled-off cost is today (near zero, since
disabled schedules mean no invocations). Turning schedules on adds:

- Lambda: a handful of scheduled invocations/day, each a few seconds to
  low minutes — at 15-20k orders/month volume (per the existing
  `serverless.yml` comments), this stays well inside the free tier
  (1M requests + 400,000 GB-s/month).
- SES: alert emails only on failure — negligible.

No new AWS resources are created by this phase.
