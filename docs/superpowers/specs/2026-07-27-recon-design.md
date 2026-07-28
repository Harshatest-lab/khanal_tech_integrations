# Recon design

**Status:** Approved (design), pending implementation.
**Repo:** `Khanal_tech_serverless`, `services/unicommerce-b2c`.
**Date:** 2026-07-27.

## Why

`Recon` is the last of the 6 real Phase 1 flows (`Fetch`, `PostInvoices`,
`PostCreditNotes`, `DashboardApi`, `PoGrn` already built and confirmed live;
`InventorySync` was built then deliberately removed as a non-requirement —
see `2026-07-27-pogrn-inventorysync-design.md`).

Our port already does *inline* reconciliation at post time:
`post_invoices.py` calls `store.update_reconciliation_diffs` right after a
new invoice posts, `credit_notes.py` calls `store.update_cn_reconciliation`
similarly. But there's a confirmed gap: the idempotency/dedup path in
`_post_order` (when `find_invoice_by_uc_order_id` finds an invoice already
exists) calls `store.mark_posted` but never calls
`update_reconciliation_diffs` — so orders that hit that path (including the
real duplicate-invoice case found live this session, DocEntry 49173/59967)
never get reconciled even though a real invoice exists in SAP. `Recon` is
the periodic sweep that catches and backfills this, and also re-verifies
already-reconciled orders in case SAP's own state has moved since (a
correction, a manual edit).

The Frappe source (`validators/reconcile.py`'s `refresh_recon`, the real
production entry point — `_run_reconciliation_refresh_legacy` is dead,
reference-only) re-derives expected totals independently via its own
GST/discount/shipping-tax formula. Read fully while designing this — see
"Key simplification" below for why we don't need to replicate that.

## Key simplification: invoice and CN need very different treatment

**Invoice**: genuinely can mismatch — SAP can round, reject, or silently
adjust something from what we submitted. Rather than re-deriving GST/
discount/shipping-tax from raw UC order data like the Frappe source does,
Recon rebuilds the *same* payload via the existing
`sap_payload.ar_invoice_payload()` (the exact function `post_invoices.py`
already calls) to get `tax_expected`/`expected_unit` — no new tax formula
needed, and it's guaranteed consistent with what was actually posted by
construction (same code path). Fetches the actual invoice from SAP
(`sap_client.get_invoice_by_doc_entry`), computes actual totals the same
way `post_invoices.py` already does. Diffs; if mismatched, runs a
line-level SKU+qty comparison to produce a human-readable reason (adapted
from the Frappe source's `_diagnose_invoice_mismatch`, but comparing
against our own payload's lines instead of re-deriving from raw UC data).

**Credit Note**: found (via an existing docstring in `store.py`, not a bug)
that expected *always* equals actual by construction for CNs — SAP Credit
Notes are "Copy From" the base invoice, so SAP fills in `UnitPrice` itself;
there's no independent value to compare against. So Recon's CN role isn't
diagnosis, it's **backfill**: re-fetch the CN from SAP, and if our row's
reconciliation was never written (e.g. skipped via a dedup path), write it
now via the existing `update_cn_reconciliation`. No mismatch-reason field
needed on the CN side.

## Scope decisions (confirmed, don't re-litigate)

- **Date window is mandatory, not optional.** Scheduled runs always use
  "current calendar month + previous calendar month" (recalculated from
  today's date every run). Manual runs can override with explicit
  `start_date`/`end_date`. No unbounded/all-time sweep exists.
- **No historical backfill pass.** Once a month is more than 1 month old,
  it's considered settled for recon purposes and never re-checked again,
  even for known gaps from before this build (e.g. the duplicate-invoice
  case). This matches how the business actually runs monthly reconciliation.
- **Mismatch-reason diagnosis is ported** (invoice side only, per above) —
  a human-readable reason string, not just diff numbers, so a mismatch is
  visible at a glance on the dashboard.
- **Handler shape matches the existing pattern**: `{"limit": N,
  "start_date"?, "end_date"?}` for the bulk sweep (defaults to the month
  window if dates omitted), `{"uniware_id": ...}` for a manual single-order
  re-check.

## Architecture

New `unicommerceRecon` Lambda, same one-flow-per-function pattern as every
other flow in this service. Scheduled (`enabled: false` initially, same
dual-run-safe pattern as everything else here), plus the manual retry mode
above.

Unlike `PoGrn`, this flow **only reads SAP and writes our own Postgres** —
no UC writes, no "no sandbox" risk, much lower stakes.

## Components

- `recon.py` (new) — `refresh_recon(conn, limit, start_date=None,
  end_date=None)` (bulk sweep) and `recon_one_order(conn, uniware_id)`
  (manual retry).
- `sap_payload.py` (extend) — promote `post_invoices.py`'s
  `_sum_unit_price_total` / `_sum_tax_total` to public functions here, so
  both `post_invoices.py` and `recon.py` share the exact same actual-totals
  computation instead of `recon.py` importing "private" functions across
  modules.
- `handlers.py` (extend) — `recon_handler`.
- `serverless.yml` (extend) — register `unicommerceRecon`, schedule
  disabled.
- `sql/unicommerce_b2c.sql` (extend) — one new column:
  `ar_invoice_mismatch_reason text` on `unicommerce_orders`. Nothing new
  needed for CN (backfill-only, no diagnosis to store).

## Data flow

**Bulk sweep** (`refresh_recon`):
1. Compute the date window: month-window default, or explicit
   `start_date`/`end_date` if passed.
2. **Invoice selection**: `unicommerce_orders` where `order_date` falls in
   the window, `pipeline_status = 'posted'`, `sap_ar_invoice_docentry !=
   ''`, and (never reconciled OR diff ≥ threshold) — prioritized
   unreconciled-first, then biggest-mismatch-first (same `ORDER BY` idea as
   the Frappe source), capped by `limit`.
3. For each: rebuild the invoice payload via `sap_payload.ar_invoice_payload`
   to get expected totals → fetch the actual invoice from SAP
   (`sap_client.get_invoice_by_doc_entry`) → compute actual totals (shared
   `sap_payload` functions) → diff → if mismatched beyond tolerance, run
   the line-level SKU+qty comparison for a mismatch reason → write via
   `store.update_reconciliation_diffs` (already exists) plus the new
   mismatch-reason column.
4. **CN selection**: same window, `sap_ar_creditnote_docentry != ''`, but
   only `ar_creditnote_reconciliation_status = ''` (never reconciled) —
   no diff-based re-check, since CN can't mismatch by construction.
5. For each: fetch the actual CN from SAP
   (`sap_client.get_credit_note_by_doc_entry`) → write via the existing
   `update_cn_reconciliation`.
6. Each order/CN attempted independently — one failure doesn't block the
   rest, same as every other bulk handler in this service.

**Manual single-order retry** (`recon_one_order`): same logic, scoped to
one `uniware_id`, bypassing the date window and limit.

## Error handling

Simpler than `PoGrn`/`InventorySync`: read-only against SAP, writes only
to our own Postgres — no claim/state-machine needed at all, no risk of a
duplicate write. A failed SAP fetch for one order is logged and skipped
this run; it stays selectable next run since nothing needs releasing.
No `failed`/`dead` status, no recovery pass.

## Testing plan

**Unit tests**: the line-level mismatch-diagnosis logic (given a payload's
lines vs SAP's actual lines, produce the right reason string), the
month-window date calculation, the priority-ordering selection logic.

**Live verification**: run against real already-posted orders on dev (we
have several from today's `PostInvoices`/`PostCreditNotes` testing) —
confirm it correctly leaves already-matched orders alone and correctly
backfills anything showing an empty reconciliation status.

## Open items for implementation

- Exact wording/format for the line-level mismatch reason string — adapt
  from the Frappe source's `_diagnose_invoice_mismatch`, simplified to our
  own payload-line shape. (Tolerance thresholds are already resolved:
  reuse the same `1.0` default `update_reconciliation_diffs` already uses.)
