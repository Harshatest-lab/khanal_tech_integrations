# Phase 0 — Decommission SafeExpress + DSR Logistics

**Confirmed:** business with both carriers has fully stopped, all
invoices/PODs settled. This is a deletion, not a migration — no Lambda
replacement is planned.

## Why first

Lowest risk, zero new infrastructure, and it shrinks the surface area
before touching anything else. It also removes noise from `hooks.py`
that would otherwise be confusing to read while working on Phase 0.5/1/2.

## What gets removed (in `khanal_tech_integrations`, the Frappe repo)

**Scheduler entries in `hooks.py`:**

```python
"30 */3 * * *":[ # SafeExpress tracking
    "khanal_tech_integrations.utils.safexpress.tracking.Update_Tracking_by_WayBill",
],
"0 */4 * * *":[ # DSR POD emails
    "khanal_tech_integrations.utils.DSR_Logisticss.DSR_POD_INTEGRATION.process_dsr_emails",
],
"30 */6 * * *":[ # DSR retry
    "khanal_tech_integrations.utils.DSR_Logisticss.DSR_POD_INTEGRATION.retry_pending_sap_invoices",
],
"40 03 * * *":[ # SafeExpress invoice attach
    "khanal_tech_integrations.utils.safexpress.automated_invoice_attach.Fetching_from_Sap",
],
```

Also check for a daily entry calling
`utils.safexpress2.pod_service.process_invoices_without_pod` (seen under
the `daily` block) — same disposition.

**Code directories:**

- `khanal_tech_integrations/utils/safexpress/` (legacy implementation)
- `khanal_tech_integrations/utils/safexpress2/` (newer implementation —
  both exist in parallel today; confirm neither is silently still used by
  something else, e.g. a doctype controller, before deleting)
- `khanal_tech_integrations/utils/DSR_Logisticss/`
- `khanal_tech_integrations/khanal_tech_integrations/doctype/safexpress_settings/`
- `khanal_tech_integrations/khanal_tech_integrations/doctype/dsr_settings/`

**Not touched:** `services/delivery-api` in this repo already handles
**Delhivery** (a different carrier) — nothing here affects it.

## Runbook

1. Grep the Frappe repo for any other reference to `safexpress`, `Safexpress`, `DSR_Logisticss`, `dsr_settings` outside the paths above (custom scripts, fixtures, reports) — a stopped-business integration can still have a leftover report or dashboard tile pointing at its doctype.
2. Remove the four `hooks.py` cron entries.
3. Delete the doctype folders and code directories listed above.
4. Restart the Frappe bench (`bench restart` or supervisor equivalent) so the scheduler picks up the new `hooks.py`.
5. Watch the Frappe error log for one full day for any stray traceback referencing the deleted modules (leftover button/report referencing the old code).
6. Commit and deploy to the Frappe EC2 box.

## Risk / rollback

- Risk is low — no live traffic depends on this code.
- Rollback: `git revert` the commit; the doctypes and cron entries come
  back as they were. No data loss risk since nothing downstream (SAP,
  Postgres) is being changed by this phase.

## Cost impact

$0 either direction — this is Frappe-side cron removal, no AWS resources
involved. The only saving is engineer attention (one less thing to reason
about) and Frappe scheduler load.
