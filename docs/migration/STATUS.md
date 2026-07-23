# Migration progress tracker

**Last updated:** 2026-07-23. Update this file as phases move — it's the
single place to check "where are we" without re-reading every phase doc.

## Right now — action items

1. **Test PR #1301** (Unicommerce fixes, `khanal_tech_integrations`) on a
   staging/dev bench — no CI exists in that repo, this is fully manual.
   Exercise all 6 flows; specifically try to trigger the 3 fixed races on
   purpose (manual retry racing a scheduler tick) to confirm they're
   actually blocked.
2. If SAP's Service Layer cert is self-signed, add
   `"sap_sl_verify_ssl": false` to `site_config.json` **before** deploying
   #1301 — otherwise the TLS fix will correctly refuse to connect until
   that's set.
3. Merge #1301, deploy to the production EC2 box manually (`git pull` /
   `bench update` + restart — no auto-deploy exists), watch one full
   cycle before moving on.
4. Merge PR #13 (`Khanal_tech_serverless`, docs-only) whenever — no
   dependency on the above.
5. Only after #1301 is stable in production: start Phase 0 (delete
   SafeExpress + DSR) and begin the Phase 1 Lambda build.
6. Separately, whenever there's time: 640 Dependabot vulnerabilities
   (7 critical) flagged on `khanal_tech_integrations`'s `main` —
   unrelated to this migration, not yet looked at.

## Repo / PR status

| Repo | Branch | PR | State |
|---|---|---|---|
| `Khanal_tech_serverless` | `worktree-typed-squishing-panda` | [#13](https://github.com/Khanal-Tech/Khanal_tech_serverless/pull/13) | Draft — migration blueprint docs (`docs/migration/*`) + serverless-repo hygiene fixes. Docs-only + config fixes, safe to merge anytime. |
| `khanal_tech_integrations` | `Unicommerce_Fixes_Yogesha_23072026` | [#1301](https://github.com/Khanal-Tech/khanal_tech_integrations/pull/1301) | Draft — TLS/permission/race fixes in the Unicommerce module. **Not deployed. Not merged. Needs manual verification first** (see action items above). |

## Phase status

| Phase | What | Status |
|---|---|---|
| Serverless repo hygiene | `layers/build` untracked, CI/deploy service-list sync, pinned layer deps, HTTP API timeout fix | ✅ Done, in PR #13 |
| Unicommerce fix pass | TLS, permission check, 3 race conditions, 1 idempotency gap — in the **existing Frappe code**, not yet the Lambda port | ✅ Fixed, in PR #1301 — **awaiting verification + deploy**, not yet merged/deployed |
| Phase 0 | Delete SafeExpress + DSR (business stopped, confirmed) | Not started — blocked behind Unicommerce fix pass landing first, per your sequencing |
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
