# PoGrn + InventorySync design

**Status:** Approved (design), pending implementation plan.
**Repo:** `Khanal_tech_serverless`, `services/unicommerce-b2c`.
**Date:** 2026-07-27.

## Why

Two of the seven Unicommerce→Lambda flows are still unbuilt: `PoGrn` (mirror a
SAP inbound stock transfer into Unicommerce as a PO + GRN, so UC's own
inventory reflects what SAP actually has) and `InventorySync` (mirror a SAP
outbound stock transfer as a UC inventory decrement). Both **write to the
live production Unicommerce tenant** — there is no UC sandbox — so this needs
to be built and tested more carefully than the read-mostly flows already
shipped.

The Frappe source has this mechanism today, but only for one warehouse pair:
inbound transfers into `EC-FG` (any source warehouse) create a UC PO+GRN;
outbound transfers `EC-FG → EC-B2C` decrement UC stock. It's triggered off a
Frappe doctype (`SAP Inventory Transfers`) that doesn't exist in this Lambda
architecture. This build extends the same mechanism to also cover all the
Amazon FBA warehouses (`AMZ-BOM5`, `AMZ-BOM7`, `AMZ-BLR7`, `AMZ-BLR5`,
`AMZ-BLR8`, `AMZ-DEL4`, `AMZ-DEL5`, `AMZ-CJB1`, `AMZ-MAA4` — the same set
`CHANNEL_WAREHOUSE_MAP` in `sap_payload.py` already maps channels to), which
doubles as the real fix for why those warehouses have had zero stock for
testing the invoice/credit-note write path.

## Architecture

Two new Lambda functions, matching the existing one-flow-per-function
pattern (`Fetch`, `PostInvoices`, `PostCreditNotes`, `DashboardApi`):

- **`unicommercePoGrn`** — polls SAP Service Layer's `StockTransfers` by
  `DocEntry` watermark, filters to lines whose destination warehouse is in
  the watched set (`EC-FG` + all `AMZ-*`), and for each new transfer: creates
  a UC PO (`createApproved`, one call), then creates the UC GRN(s) against it
  (round-robin batching for UC's "no duplicate SKU per GRN" rule), using the
  batch numbers straight from the SAP transfer document.
- **`unicommerceInventorySync`** — polls the same `StockTransfers` feed for
  the outward side: `EC-FG → EC-B2C` specifically, or *any* transfer out of
  an `AMZ-*` warehouse to anywhere. Decrements UC stock via
  `inventory/adjust/bulk` (`REMOVE`).

Both are scheduled (`enabled: false` initially, same dual-run-safe pattern as
every other flow here), plus a manual single-`DocEntry` retry mode like the
existing `{"uniware_id": ...}` pattern on `PostInvoices`.

**Trigger mechanism:** SAP Service Layer's `StockTransfers` entity, not raw
HANA. Confirmed live against `india_test`: list queries (`$filter=DocEntry
gt N`) don't include `BatchNumbers` in the line summary, but the full
per-document `GET StockTransfers(docEntry)` does — same two-step
list-then-detail pattern `fetch_sync.py` already uses for UC orders. Real
historical transfers into both `EC-FG` and `AMZ-BLR7` already exist in
`india_test`, confirming the mechanism is live and testable without any
synthetic setup.

**Cursor:** `DocEntry`-based watermark (SAP's `DocEntry` is a strictly
increasing sequence — no overlap-window/timestamp-drift logic needed, unlike
`fetch_sync.py`'s UC-side date cursor). Two independent watermarks
(`pogrn_transfers`, `inventory_sync_transfers`) — kept independent since the
two flows are separate Lambdas with separate schedules, not a shared cursor.
The watermark advances based on "evaluated" (a staging row was created for
that `DocEntry`), not "succeeded" — so a failed transfer isn't silently
skipped by the polling cursor; failed/stuck rows get recovered by a separate
pass each run (see Error handling).

## Components

New source files, mirroring the existing module split:

- `stock_transfers.py` — SAP SL `StockTransfers` polling (list by watermark)
  + full-detail fetch (for `BatchNumbers`), shared by both flows.
- `pogrn.py` — inward orchestration: claim → create UC PO → create UC
  GRN(s).
- `inventory_sync.py` — outward orchestration: claim → decrement UC stock.
- `uc_client.py` (extend existing) — new write methods:
  `create_po_approved`, `create_inflow_receipt`, `add_grn_item`,
  `adjust_inventory_bulk`.
- `store.py` (extend) — CRUD + atomic-claim functions for the two new
  tables.
- `handlers.py` (extend) — `pogrn_handler`, `inventory_sync_handler`.
- `serverless.yml` (extend) — register both functions, schedules disabled.

## Data model

Two new Postgres tables, one row per SAP transfer `DocEntry`:

**`unicommerce_pogrn_staging`**
- `sap_doc_entry` (PK), `from_warehouse`, `to_warehouse`
- `line_items` (jsonb) — snapshot of items/batches/qty captured at claim
  time, so retries don't need to re-hit SAP
- `status`: `pending → po_creating → po_created → grn_creating → grn_done`,
  plus `failed` / `dead`
- `uc_po_id`, `uc_grn_ids` (jsonb array — GRN can span multiple rounds,
  appended to as each round succeeds)
- `attempt_count`, `last_error`, timestamps

**`unicommerce_inventory_sync_staging`** — same shape, simpler state machine
(`pending → syncing → synced`, plus `failed`/`dead`), no PO/GRN id fields.

**Warehouse config:** no new settings needed. The watched destination set is
`{DEFAULT_WAREHOUSE} | set(CHANNEL_WAREHOUSE_MAP.values())`
(`sap_payload.py`'s existing constants — single source of truth). The
SAP-warehouse → UC-facility-code mapping needed for PO/GRN payloads comes
from **inverting `settings.warehouse_map`** — its existing keys
(`"AMAZON_FBA_IN_BLR7"`, `"kfl_ecom_warehouse"`, etc.) already are the UC
facility identifiers. Vendor code for the PO reuses `settings.vendor_code`
(already `"EC-DIS"`).

## Data flow

**PoGrn (inward):**
1. Poll `StockTransfers` past the watermark, filter to lines whose
   destination is in the watched warehouse set.
2. Claim (insert/claim row in `unicommerce_pogrn_staging`).
3. Fetch full detail for `BatchNumbers`, snapshot into the row.
4. Build UC PO: qty aggregated per SKU (UC forbids duplicate-SKU rows),
   facility code via inverted `warehouse_map`, vendor from
   `settings.vendor_code`.
5. `createApproved` → **commit `uc_po_id` immediately** (its own committed
   step, before GRN creation starts).
6. Build GRN round(s): round-robin split by unique SKU per round (mirrors
   the Frappe source's `_push_grn_rounds`), batch numbers straight from the
   snapshot — no fresh SAP query needed.
7. Create each round, appending to `uc_grn_ids` as each one succeeds (a
   mid-rounds crash only re-attempts what didn't finish).
8. All rounds done → `grn_done`.

**InventorySync (outward):** same poll/claim/snapshot shape, filtered to
`EC-FG → EC-B2C` specifically, or any transfer out of an `AMZ-*` warehouse →
one `inventory/adjust/bulk` REMOVE entry per line, using the batch/qty
straight from the SAP transfer snapshot (no UC shelf-stock lookup needed at
all, unlike the Frappe source's fragile string-match against UC's shelf
inventory — we already have the authoritative batch from SAP itself) →
`synced`.

## Error handling

Reuses this codebase's proven idiom directly: atomic conditional-UPDATE
claim with a sentinel in-flight status (`po_creating`, `grn_creating`,
`syncing`), released on **any** exception (not just the expected-failure
branch) — the exact lesson from three real production incidents in the
Frappe source (records `54661`, `24798` both got stuck because an
under-granular claim wasn't released on unexpected exceptions). `max_attempts`
/ `max_hours` cap before marking `dead`, mirroring `PostInvoices`'s existing
`BATCH_HOLD_MAX_ATTEMPTS` / `BATCH_HOLD_MAX_HOURS`. Recovery of `failed` rows
is a separate pass each run (`recover_failed_transfers`), matching
`PostInvoices`'s existing `recover_failed_orders` / `recover_batch_hold_orders`.

## Scope decisions (confirmed, don't re-litigate)

- **Manual-PO-fill use case** (filling a GRN for a UC PO created manually
  inside UC itself, not from a SAP transfer) — **out of scope** for this
  build. Automatic SAP-transfer-triggered flow only.
- **AMZ-\* rollout** — build to watch all warehouses in the set from day
  one (same code path per warehouse, no extra complexity to scope down);
  deploy with schedule disabled like every other cutover here.
- **InventorySync batch-matching** — upgraded from the Frappe source's
  fragile string-match, using the batch number straight from the SAP
  transfer document (already confirmed via `StockTransfers`' `BatchNumbers`)
  instead.
- **Crash recovery between PO and GRN creation** — state persisted after
  each write (not a single try/except spanning both), so a retry never
  re-issues a UC write that already succeeded.

## Testing plan

**Unit tests** (pure logic, no real UC/SAP calls, matching this repo's
existing style): PO-payload building (qty aggregation per SKU), GRN
round-robin batching (duplicate-SKU splitting), facility-code inversion,
warehouse-filter logic (`EC-FG→EC-B2C` vs. any-destination-from-`AMZ-*`),
claim/state-transition logic — same monkeypatch style already used for
`sap_client.py`.

**Live verification, staged carefully:**
- **Phase A** — both handlers take a `dry_run: bool` parameter (matching
  `amazon-us-b2c/post_invoices.py`'s existing `dry_run` precedent, not a
  one-off manual edit): computes the real PO/GRN or adjust payload from
  live SAP data, but never claims or calls UC. Deploy with schedules
  disabled, invoke with `limit=1, dry_run=true` against real SAP data
  (transfers into `EC-FG` and `AMZ-BLR7` already confirmed to exist in
  `india_test`) to visually confirm the payload is correct before ever
  touching prod UC.
- **Phase B** — one deliberately tiny, low-impact real test against prod
  UC (either the smallest existing historical transfer, or a fresh tiny
  real SAP transfer as a known controlled input), reviewed before it runs.
- Confirm the result via UC's own read APIs afterward
  (`getPurchaseOrderDetails`, `itemTypeInventory/get`), same
  "verify against the real source system" discipline used all session for
  SAP.

## Open items for the implementation plan

- Exact UC API request/response shapes for `createApproved`,
  `inflowReceipt/create`, `inflowReceipt/addItemSKU`,
  `inventory/adjust/bulk` — port from `client/unicommerce.py` in the Frappe
  source (`UCClient`'s OAuth password-grant login with token caching,
  `_post_with_reauth`/`_get_with_reauth` auto-relogin-on-401 pattern).
- Exact Postgres migration/DDL for the two new tables.
- Whether a new dashboard endpoint is needed to surface `dead` rows for
  human review, or whether that's a later follow-up.
