# Migration progress tracker

**Last updated:** 2026-07-27. Update this file as phases move — it's the
single place to check "where are we" without re-reading every phase doc.

## Right now — action items

1. **Phase 1 (`services/unicommerce-b2c`) is deployed to the `dev` stack
   and the full write path is now confirmed real, live, end-to-end**
   (2026-07-27): fetch → promote → batch-check → series guard →
   `POST /Invoices` → `POST /CreditNotes`, all verified directly against
   `TEST_KFL_30062026` (SAP SL + HANA), not just our own DB records.
   **All 6 of 6 real Phase 1 flows now built and deployed** (`Fetch`,
   `PostInvoices`, `PostCreditNotes`, `DashboardApi`, `PoGrn`, `Recon`,
   plus the underlying HANA/SAP client layer) — Phase 1 build is
   complete.
   **`Recon` built, deployed, and confirmed live the same day**: a
   periodic re-verification sweep, scoped to a rolling
   current+previous-calendar-month window (recalculated fresh every
   run — no unbounded sweep, no historical backfill). Rebuilds the
   invoice payload via the same `sap_payload.ar_invoice_payload` builder
   `PostInvoices` uses (so "expected" is guaranteed consistent with what
   was actually submitted), re-fetches the real posted SAP AR Invoice,
   diffs tax + unit-price totals, and — when they disagree — writes a
   human-readable line-level mismatch reason (adapted from the Frappe
   source's `_diagnose_invoice_mismatch`) into a new
   `ar_invoice_mismatch_reason` column. Credit notes are backfill-only
   (SAP CNs are "Copy From" the base invoice, so expected == actual by
   construction there — nothing to diagnose). This closes a real gap:
   the idempotency-dedup path in `PostInvoices` marks an order posted
   without ever computing its reconciliation diff, so orders that hit
   that path — including the real duplicate-invoice case found live
   this session (DocEntry 49173/59967) — previously had a real SAP
   invoice but no recorded diff. Live-verified via the manual
   single-order path (bypasses the month window) against
   `280945263450753472_1_FORWARD` (DocEntry 59967): rebuilt payload
   matched the real SAP invoice exactly (tax 16.33 vs 16.33, unit price
   326.67 vs 326.67, diff 0.00 both), `updated_at` timestamp confirmed
   the write was fresh, not stale data. Design doc:
   `docs/superpowers/specs/2026-07-27-recon-design.md`. Deployed with
   `enabled: false` on its schedule, same dual-run-safe pattern as
   every other cutover here — read-only against SAP so there's no
   real-write risk, but still held disabled pending the same live
   confidence-building as the rest of Phase 1.
   **`PoGrn` built, tested, deployed, and confirmed with a real live
   write** the same day: real PO `PO8975` created in production UC
   (vendor `EC-B2C`, `FGHN0059` qty 240 + `FGHN0035` qty 96, matching SAP
   transfer DocEntry 53974 exactly) + 2 real GRNs, fully received
   (0 pending on both lines) — verified via UC's own
   `getPurchaseOrderDetails`, not just our DB.
   **`InventorySync` was built the same day, then deliberately removed
   after real-write testing found it solves a problem that doesn't
   exist**: `EC-FG`/`EC-B2C`/every `AMZ-*` warehouse are same-company
   internal warehouses; Unicommerce already decrements a batch's
   inventory itself the moment a sale order ships (no action needed from
   us); `PostInvoices` (posting the SAP AR Invoice for that same order)
   is what corresponds to the decrease, already built and verified live.
   A manual outward `inventory/adjust/bulk` REMOVE — which is what
   `InventorySync` did — would have **double-decremented** real UC
   inventory for stock UC had already correctly reduced on its own. Full
   reasoning + the live evidence trail (chasing UC's undiscoverable
   batch-code requirement is what surfaced this) is in
   `docs/superpowers/specs/2026-07-27-pogrn-inventorysync-design.md`'s
   Correction section. So Phase 1 was never actually 7 distinct flows —
   `InventorySync` wasn't a real requirement once UC's own
   fulfillment-driven decrement is accounted for.
   **5 more real bugs found + fixed live today** (on top of the
   `InventorySync` finding above), **all deployed to `dev`:**
   - `hana_access.bulk_manage_batch_numbers` queried HANA column
     `ManageBatchNumbers` — that's the SAP Service Layer property name,
     not the real column (`ManBtchNum`). Silently broke batch-managed
     lookups in every environment, not just this one.
   - `validate_active_series` (the FY-rollover safety guard — exists
     because of a real prior incident, 148 cancelled March orders nearly
     double-booked into a stale series) queried a `Series` OData entity
     that doesn't exist in this SAP SL version at all, and had an
     inverted FY-suffix calculation on top — it had **never actually
     validated anything**, including in the Frappe source it was ported
     from. Rebuilt to check the actual series IDs configured in
     Unicommerce Settings (`ar_invoice_series_map` etc.) via
     `SeriesService_GetSeries`, the correct working RPC.
   - `parse_source_datetime` crashed `fetch_returns` live
     (`TypeError: can't compare offset-naive and offset-aware datetimes`)
     on UC return timestamps sent without a UTC offset.
   - `find_invoice_by_uc_order_id` / `find_credit_note_by_return_id`
     queried header UDFs (`U_UCOrderID`, `U_UCReturnID`) that don't exist
     on this SAP install at all (confirmed via SAP SL rejecting them
     outright, and via `SYS.TABLE_COLUMNS` — not a naming typo, genuinely
     absent) and **raised instead of gracefully degrading** like the
     Frappe source does, so the dedup guard did nothing — confirmed this
     caused a real duplicate invoice in SAP (DocEntry 49173 + 59967, same
     UC order, both live). Fixed by checking HANA directly instead:
     `INV1.U_Order` (line-level UDF the payload builder already sets
     correctly) for invoices, `RIN1.BaseEntry`/`BaseType` (the standard
     SAP "Copy From" link, no custom field needed) for credit notes. No
     UDF creation needed — an earlier plan to add `U_UCOrderID` via the
     SAP B1 client is superseded, skip it.
   - `post_ready_orders`'s result-counter loop had no branch for the
     `"failed"` outcome — orders still recorded correctly in Postgres,
     but silently missing from the Lambda's own returned summary.
   **Stock note:** `india_test` has real batch stock at the default
   `EC-FG` warehouse (used by every non-Amazon-FBA channel — Flipkart,
   Meesho, etc.), which is what made live write-path testing possible
   without any manual data load. Amazon-FBA-channel orders (routed to
   their own dedicated `AMZ-*` warehouses) still have zero stock there
   and remain genuinely untested on the write path — that's a data gap
   in the test company, not a code issue.
2. **PR #13 merged 2026-07-24. This branch kept 35 more commits on top of
   that (PoGrn, InventorySync build+removal, Recon, 14 live bugfixes)
   that were never merged** — reconciled with `main` (2 unrelated
   `delivery-api` "fixed the delivery date" commits main had gained
   meanwhile) and opened as
   [PR #16](https://github.com/Khanal-Tech/Khanal_tech_serverless/pull/16),
   2026-07-28. 190 tests pass. **Not yet merged** — merging it directly
   was blocked by the auto-mode classifier (shared-state action), needs
   a human click on the PR.
3. **Go-live target: 2026-08-01**, per user (2026-07-28). Before then,
   still needed: prod SSM params (`/unicommerce/api_credentials`,
   `/unicommerce/settings`, default `/sap/*` — currently only the
   `india_test` profile is populated), prod deploy (schedules still
   `enabled: false`), then the parallel fetch-verification window and
   staged write cutover per the Cutover runbook below — none of that
   has started yet, so 2026-08-01 is tight for anything beyond "prod
   stack exists but not cut over."
4. **Kiosk-frontend repoint (runbook step 6) is cancelled** — user
   decided (2026-07-28) kiosk-frontend won't be used going forward. A
   new dashboard will be built directly against `unicommerceDashboardApi`
   instead (same HTTP API + Cognito pool already live-verified on dev).
   Not started; not a go-live blocker since the existing Frappe
   dashboard keeps working until Phase 1 write cutover completes.
5. **Warehouse/inventory snapshot pulled directly from `india_test`
   HANA (2026-07-28)**, to check the AMZ-\* stock gap noted below:
   `EC-FG` 14,983 on hand; of the 9 `AMZ-*` facility warehouses,
   7 are still genuinely at 0 (`AMZ-BLR7`, `AMZ-BLR8`, `AMZ-CJB1`,
   `AMZ-DEL4`, `AMZ-DEL5`, `AMZ-MAA4`, `AMZ-PNQ3`) but **`AMZ-BOM5` (4)
   and `AMZ-BOM7` (207) now have real stock** — likely residual from
   the `PoGrn` live-write test (PO8975). Worth a targeted
   `PostInvoices` dry-run against SKUs in those two facilities to get
   partial live coverage on the outward write path before go-live,
   since full AMZ-\* coverage still isn't possible without more test
   stock loaded.
6. **Resolved (2026-07-28)**: user's "InventorySync should only
   increment, not decrement" is already fully satisfied by `PoGrn`
   (SAP transfer → UC PO+GRN, increases UC stock) — confirmed with the
   user this was a restatement of the existing design, not a request
   for new code. The deleted `InventorySync` (decrement/REMOVE side)
   stays deleted, per the 2026-07-27 finding that it double-decrements
   (UC already lowers stock itself on order ship). No action needed.
7. Separately, whenever there's time: 640 Dependabot vulnerabilities
   (7 critical) flagged on `khanal_tech_integrations`'s `main` —
   unrelated to this migration, not yet looked at.
8. Separately: `amazon_us` module has the same hardcoded
   `SAP_VERIFY_SSL = False` issue Unicommerce had (`amazon_us/__init__.py:5`)
   — offered to fix, no answer yet from the user.

## Phase 0 investigation notes (paused, not abandoned)

Before reprioritizing, a pre-deletion sweep of `khanal_tech_integrations`
found real dependencies outside the paths `01-decommission-safexpress-dsr.md`
originally scoped for deletion — worth re-reading this before resuming
Phase 0 later, so the investigation isn't redone from scratch:

- `doctype/sap_ar_invoice_detail/sap_ar_invoice_detail.py` imports
  `Saved_InvDoc` from `utils/safexpress/automated_invoice_attach.py` —
  a real import in a doctype **controller**. Unclear yet whether `SAP AR
  Invoice Detail` is used for anything beyond SafeExpress POD tracking.
- `utils/cashfree/vrf/gst_verification.py` imports
  `Update_waybill_and_filePath` from `utils/safexpress/attachpod_to_sap.py`
  — confirmed (read the function) genuinely SafeExpress-specific, not a
  misplaced generic helper; it calls SafeExpress's live tracking API
  directly. Unclear whether GST verification's use of this is still active.
- `utils/repatch_pod.py` — a POD-repair utility with dedicated SafeExpress
  and DSR repatch functions (lazy-imported, won't crash on import, but the
  functionality dies on deletion).
- `api/logistics_dashboard.py` — has live trigger functions
  (`trigger_safexpress_pod_sync`, DSR email processing) — likely dashboard
  buttons ops staff can still click.
- `www/intranet/myorder.py` — a customer-facing "track my order" page
  hitting SafeExpress's live tracking API directly.

None of this blocks Phase 1. When Phase 0 resumes, get answers on these
five points before deleting anything — see the paused `AskUserQuestion`
in the conversation history for the exact questions.

## Repo / PR status

| Repo | Branch | PR | State |
|---|---|---|---|
| `Khanal_tech_serverless` | `worktree-typed-squishing-panda` | [#13](https://github.com/Khanal-Tech/Khanal_tech_serverless/pull/13) | Draft — migration blueprint docs + serverless-repo hygiene fixes + Phase 1 build complete (`services/unicommerce-b2c`, 6 of 6 flows built + live-verified). Safe to merge anytime — nothing in it deploys automatically. |
| `khanal_tech_integrations` | `Unicommerce_Fixes_Yogesha_23072026` | [#1301](https://github.com/Khanal-Tech/khanal_tech_integrations/pull/1301) | ✅ Merged + deployed to production (`khanaltech.com`), 2026-07-23. |
| `khanal_tech_integrations` | `ApiLogError_Fix_Yogesha_23072026` | [#1302](https://github.com/Khanal-Tech/khanal_tech_integrations/pull/1302) | ✅ Merged + deployed. Fixed a `frappe.log_error` arg-order crash found while verifying #1301. |
| `khanal_tech_integrations` | `PoGrn_ClaimRelease_Fix_Yogesha_23072026` | [#1303](https://github.com/Khanal-Tech/khanal_tech_integrations/pull/1303) | ✅ Merged + deployed (1 of 2 commits — see #1304). Fixed claim-not-released-on-exception found while verifying #1301. |
| `khanal_tech_integrations` | `PoGrn_ClaimRelease_Fix_Yogesha_23072026` | [#1304](https://github.com/Khanal-Tech/khanal_tech_integrations/pull/1304) | ✅ Merged + deployed. #1303's second commit, split out after a merge-timing gap — fixed a `comments`-field-length truncation bug (500 assumed, 140 actual) found while re-verifying #1303. |

**All four PRs above are merged, deployed to production, and manually verified live** (TLS, permission check, 3 atomic claims, 1 idempotency gap, plus the 2 bugs found along the way) — see memory file `frappe_exit_migration_program.md` for full verification detail. Two stuck production records (`54661`, `24798`) were reset as part of this. Unicommerce fix pass is **done**.

## Phase status

| Phase | What | Status |
|---|---|---|
| Serverless repo hygiene | `layers/build` untracked, CI/deploy service-list sync, pinned layer deps, HTTP API timeout fix | ✅ Done, in PR #13 |
| Unicommerce fix pass | TLS, permission check, 3 race conditions + 2 bugs found while verifying, 1 idempotency gap — in the **existing Frappe code**, not yet the Lambda port | ✅ **Done** — merged, deployed, verified live in production (PRs #1301–#1304) |
| Phase 0 | Delete SafeExpress + DSR (business stopped, confirmed) | **Paused** — investigation started, found real cross-module dependencies (see notes above), deprioritized behind Phase 1 per user's 2026-07-23 call |
| Phase 0.5 | Finish the half-built Amazon US Lambda cutover (test DB creds, schedules disabled) | Not started |
| Phase 1 | Unicommerce → Lambda (6 real flows, not 7 — see note below and `03-unicommerce-b2c.md`) | **6 of 6 flows built + live-verified on `dev`, full write path confirmed real (2026-07-27).** See memory `unicommerce_lambda_dev_verification.md`. ✅ `Fetch` orders + returns · ✅ `PostInvoices` — **real `POST /Invoices` confirmed**, verified directly against SAP (DocEntry 59967/DocNum 702697) · ✅ `PostCreditNotes` — **real `POST /CreditNotes` confirmed**, correctly linked to its parent invoice via `BaseEntry` (DocEntry 10807/DocNum 67373) · ✅ `DashboardApi` · ✅ `PoGrn` — **real UC Purchase Order + GRN confirmed**, both AMZ-\* and EC-FG destinations tested, a real write fully received (PO8975, DocEntry 53974, 0 pending). **`InventorySync` built then deliberately removed same day** — not a real requirement: UC already decrements inventory itself on order fulfillment, `PostInvoices` already covers the SAP-side correspondence; a manual outward REMOVE would have double-decremented real inventory. Full reasoning in `docs/superpowers/specs/2026-07-27-pogrn-inventorysync-design.md`'s Correction section. **14 real bugs found + fixed live across two sessions**, none caught by unit tests: 2026-07-24 — missing promote gate; UC 31-day search limit swallowed as 0 results; NULL-vs-`''` NOT NULL columns; `get_cn` json.loads on jsonb; empty GitHub-secret env shadowing. 2026-07-27 — wrong HANA column name in batch-managed lookup; FY series guard queried a nonexistent SAP entity + had an inverted FY-suffix calc (never validated anything, in the Frappe source either); `fetch_returns` crashed on offset-naive UC timestamps; invoice/CN dedup queried nonexistent header UDFs and silently did nothing, causing a real duplicate invoice in SAP (fixed via HANA `INV1.U_Order`/`RIN1.BaseEntry` instead — no UDF creation needed); a result-counter bug dropping `"failed"` outcomes; GRN batch-group field mismatch (`mfd` rejected); the InventorySync design gap itself. **Stock note:** `EC-FG` (default warehouse, every non-Amazon-FBA channel) has real batch stock; `AMZ-*` warehouses now also proven reachable for real writes via `PoGrn` (confirmed live against DocEntry 53819). ✅ `Recon` — **periodic re-verification sweep, built and live-verified same day**: rebuilds the invoice payload via the same builder `PostInvoices` uses, re-fetches the real SAP invoice, diffs, and writes a line-level mismatch reason on disagreement (CN side is backfill-only — expected == actual by construction). Closes the real gap where the idempotency-dedup path never computed a diff. Live-verified via the manual single-order path against DocEntry 59967 — rebuilt payload matched SAP exactly. Deployed disabled, same dual-run-safe pattern as the rest of Phase 1. Design: `docs/superpowers/specs/2026-07-27-recon-design.md`. **Phase 1 build is now fully complete — all 6 of 6 real flows built, deployed, and live-verified.** `PoGrn`'s manual-PO-fill use case and any future UC-write flows (e.g. actual PO/GRN creation triggered by something other than a SAP transfer) remain out of scope per the approved design. |
| Phase 2 | B2B PO automation (6 marketplaces, IMAP-based) → Lambda | Not started. Open question: confirm Flipkart is actually scheduled (not found in `hooks.py`'s cron block) |
| Phase 3a | consumables → Lambda + Postgres | Not started |
| Phase 3b | real_aquatics, wowpets → Lambda + Postgres (both live, daily users — higher stakes) | Not started. Order between the two not yet decided |
| Phase 4 | Apache Superset reporting | Not started. Independent of everything else — can start anytime |
| Phase 5 | Decommission Frappe + EC2 | Not started — last phase, depends on everything above |

## Decisions made so far (don't re-litigate these)

- End state: full Frappe exit, including wowpets/real_aquatics/consumables — not just the integration workers.
- No hard deadline — phase everything safely, one integration at a time, dual-run before cutover.
- Superset: small EC2/Lightsail or ECS Fargate (final call TBD at Phase 4 kickoff), querying Postgres + HANA directly, no ETL layer.
- consumables goes first in Phase 3 (lower external-user exposure than real_aquatics/wowpets, which are both live daily).
- Unicommerce bug-fix pass done **before** touching Phase 0, per your explicit reordering request.

## Cross-references

- Full architecture blueprint: [00-OVERVIEW.md](00-OVERVIEW.md) and the numbered phase docs in this directory.
- Repo topology, local paths, why two repos are in play: memory file `frappe_exit_migration_program.md`.
- Serverless repo hygiene findings: memory file `serverless_repo_hygiene_fixes.md`.
