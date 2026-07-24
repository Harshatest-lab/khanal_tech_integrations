# Phase 5 — Decommission Frappe + EC2

Only starts after Phase 3b (real_aquatics, wowpets) is fully migrated and
their rollback windows have passed cleanly — this is the last phase by
definition, since it removes the thing every earlier phase ran alongside.

## Preconditions (all must be true)

- Phase 0: SafeExpress + DSR code deleted.
- Phase 0.5: Amazon US fully on Lambda, Frappe `amazon_us` module deleted.
- Phase 1: Unicommerce fully on Lambda, Frappe `unicommerce/` deleted.
- Phase 2: All B2B marketplaces (five or six, per the Flipkart question in
  [04-b2b-po-automation.md](04-b2b-po-automation.md)) on Lambda, Frappe
  `utils/B2B/` deleted.
- Phase 3a/3b: consumables, real_aquatics, wowpets all cut over, rollback
  windows passed with no incidents, Frappe apps for all three
  decommissioned.
- Nothing left in `khanal_tech_integrations`'s `hooks.py` scheduler that
  isn't already accounted for above — **re-check `hooks.py` at this point
  rather than trusting this list**, since new crons could have been added
  to the Frappe app during the (likely months-long) migration window.

## What this phase actually does

1. **Full data export/archive** of anything in the Frappe MariaDB/Postgres
   instance not already mirrored in the Lambda-side Postgres schemas —
   for compliance/audit retention, not for active use. Export as SQL dump
   or equivalent, store in S3 with appropriate retention/lifecycle policy
   (Glacier after N months, per whatever retention period is actually
   required — this wasn't specified and should be confirmed rather than
   assumed).
2. **Snapshot the EC2 instance** (AMI) before termination, retained for
   some defined period, in case something unexpected surfaces after
   decommission that needs the original environment to diagnose.
3. **Confirm no other consumer** depends on the Frappe box — check for
   anything not covered by this blueprint: cron jobs, webhooks pointing at
   Frappe's URL, DNS records, other services with hardcoded Frappe
   endpoints. The `hooks.py` scheduler audit in this repo covered
   integrations and the three apps; things like `custom_scripts/`,
   `patches/`, one-off `page/` or `web_form/` entries, or external
   webhooks calling into Frappe's REST API weren't exhaustively covered
   this pass — do a final sweep before terminating anything.
4. **Terminate the EC2 instance**, release its Elastic IP if any, remove
   its security groups, remove any Route53/DNS records pointing at it.
5. **Remove Frappe-specific IAM** if the EC2 instance had an instance
   profile/role — clean up rather than leave orphaned.
6. **Update this blueprint's status** in
   [00-OVERVIEW.md](00-OVERVIEW.md) to reflect completion.

## Risks

- **The biggest risk in this phase is discovering a dependency you didn't
  know about** — hence step 3 being explicit and separate rather than
  assumed covered by "everything in this blueprint is done." Frappe apps
  accumulate ad-hoc scripts and one-off pages over years; a
  blueprint written from a point-in-time repo read (this one, dated
  2026-07-23) can't guarantee it caught everything that existed then, let
  alone anything added during the migration itself.
- Irreversible once the EC2 instance and its data are actually gone — the
  AMI snapshot and data export in steps 1–2 are the safety net; don't
  skip them to save a small amount of S3/snapshot cost.

## Cost impact

This is the phase that actually delivers savings, not spends money:

- **Removes**: whatever the Frappe EC2 instance costs today. **This
  number wasn't available from the repos** (it's AWS console/billing
  data, not code) — get the current instance type/size and monthly cost
  from the AWS console before this phase, so the savings are a real
  number instead of a placeholder.
- **Removes**: any RDS/MariaDB instance backing Frappe specifically, if
  separate from the shared Postgres RDS instance the Lambda services use
  — confirm which database Frappe currently uses (the existing docs
  reference "Frappe writes tracking snapshots to Postgres" for Delhivery,
  suggesting some shared Postgres usage already, but Frappe's own
  doctypes are very likely on a separate MariaDB instance typical of
  Frappe deployments — verify rather than assume).
- **Adds**: S3 storage for the archived export + AMI snapshot storage —
  likely a few dollars/month depending on data volume and retention
  length, dropping further if lifecycle-policied to Glacier after the
  initial retention window.

**Net effect: this phase should be net cost-negative (savings), by
whatever the Frappe EC2 + its DB currently cost, minus a few dollars/month
for archival storage.** Get the actual EC2/DB cost figure before treating
this as a concrete savings number in any budget conversation.
