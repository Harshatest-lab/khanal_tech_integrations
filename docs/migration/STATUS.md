# Migration progress tracker

**Last updated:** 2026-07-23. Update this file as phases move — it's the
single place to check "where are we" without re-reading every phase doc.

## Right now — action items

1. **Phase 1 build in progress** (`Khanal_tech_serverless`, same PR #13
   branch). 3 of 7 functions done: `unicommerceFetch`,
   `unicommercePostInvoices`, `unicommercePostCreditNotes`. Packages
   clean, registered in ci.yml + deploy.yml + pytest.ini. **Not deployed
   anywhere** (schedule `enabled: false` on all three, same dual-run-safe
   pattern as every other cutover). New per-service SSM params needed
   before any real deploy: `/unicommerce/api_credentials`,
   `/unicommerce/settings` (replaces the Frappe "Unicommerce Settings"
   doctype — see `settings.py`'s docstring for the expected JSON shape).
   4 functions remain: PO/GRN creation, inventory sync, reconciliation,
   dashboard API — see `03-unicommerce-b2c.md`'s function table.
   Deliberate scope decisions made so far (all documented in-code):
   kept one inherited Frappe bug (`quantity: 0` treated as `1` in
   `mappers.eligible_line_items`) for dual-run fidelity, not yet fixed;
   skipped the "EC-FG FEFO rescue" optimization and the SQL
   reservation-ledger audit table (both non-functional niceties);
   credit-note posting has no data to process yet in practice —
   `unicommerce_cn_staging` only gets populated by `fetch_returns`
   (part of `unicommerceFetch`'s remaining scope, not yet built), so the
   worker is correct but untested end-to-end until that lands; found and
   fixed a real bug while building this — `unicommercePostInvoices`'s
   `store.claim_ready_order()` calls were missing the required `conn`
   argument (both call sites), which would have crashed every posting
   run — caught because credit-note posting reads the same `store.py`
   patterns and there's still no test coverage for `post_invoices.py`
   itself (only its dependencies) to have caught it earlier.
2. Merge PR #13 (`Khanal_tech_serverless`, docs-only + Phase 1 code so
   far) whenever — no dependency on anything else, and nothing in it
   deploys automatically (no CI/CD auto-deploy exists in either repo).
3. Separately, whenever there's time: 640 Dependabot vulnerabilities
   (7 critical) flagged on `khanal_tech_integrations`'s `main` —
   unrelated to this migration, not yet looked at.
4. Separately: `amazon_us` module has the same hardcoded
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
| `Khanal_tech_serverless` | `worktree-typed-squishing-panda` | [#13](https://github.com/Khanal-Tech/Khanal_tech_serverless/pull/13) | Draft — migration blueprint docs + serverless-repo hygiene fixes + Phase 1 build in progress (`services/unicommerce-b2c`, 1 of 7 functions done). Safe to merge anytime — nothing in it deploys automatically. |
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
| Phase 1 | Unicommerce → Lambda (7 flows — see `03-unicommerce-b2c.md`) | **5 of 7 flows built + live-verified on `dev` (2026-07-24). See memory `unicommerce_lambda_dev_verification.md`.** ✅ `Fetch` orders + returns · ✅ `PostInvoices` (promote→series guard→HANA batch→graceful `batch_hold`) · ✅ `PostCreditNotes` orchestration · ✅ `DashboardApi` (read-only HTTP+Cognito, all endpoints verified vs staged data). **5 real bugs found + fixed live** (missing promote gate; UC 31-day search limit swallowed as 0 results; NULL-vs-`''` NOT NULL columns; `get_cn` json.loads on jsonb; empty GitHub-secret env shadowing) — none caught by unit tests. **Verification wall:** test company has no HANA stock → no invoice can actually post, so the SAP-write depth of PostInvoices/PostCreditNotes + all of `Recon` can't be exercised until stock is loaded into test HANA. **Remaining 2 flows:** `Recon` (not ported; unverifiable here anyway), `PoGrn` + `InventorySync` (not ported — WRITE to the live UC tenant, no sandbox → held per user's 2026-07-24 call). |
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
