# Migration progress tracker

**Last updated:** 2026-07-23. Update this file as phases move — it's the
single place to check "where are we" without re-reading every phase doc.

## Right now — action items

1. **Start Phase 0** — delete SafeExpress + DSR code (business stopped,
   confirmed). Unicommerce fix pass is done and verified, no longer
   blocking this.
2. Merge PR #13 (`Khanal_tech_serverless`, docs-only) whenever — no
   dependency on anything else.
3. Separately, whenever there's time: 640 Dependabot vulnerabilities
   (7 critical) flagged on `khanal_tech_integrations`'s `main` —
   unrelated to this migration, not yet looked at.
4. Separately: `amazon_us` module has the same hardcoded
   `SAP_VERIFY_SSL = False` issue Unicommerce had (`amazon_us/__init__.py:5`)
   — offered to fix, no answer yet from the user.

## Repo / PR status

| Repo | Branch | PR | State |
|---|---|---|---|
| `Khanal_tech_serverless` | `worktree-typed-squishing-panda` | [#13](https://github.com/Khanal-Tech/Khanal_tech_serverless/pull/13) | Draft — migration blueprint docs (`docs/migration/*`) + serverless-repo hygiene fixes. Docs-only + config fixes, safe to merge anytime. |
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
| Phase 0 | Delete SafeExpress + DSR (business stopped, confirmed) | **Starting now** |
| Phase 0.5 | Finish the half-built Amazon US Lambda cutover (test DB creds, schedules disabled) | Not started |
| Phase 1 | Unicommerce → Lambda (7 functions — see `03-unicommerce-b2c.md`) | Not started. Now scoped in detail (6 real flows + recon + ops layer, confirmed by reading the actual code, not just file names) |
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
