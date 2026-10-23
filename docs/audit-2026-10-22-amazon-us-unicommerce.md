# Audit: amazon-us-b2c + unicommerce-b2c (2026-10-22)

Production audit: **58/100, Risky**. Posting logic is mature and idempotent; auth, alerting, TLS and ops hygiene are not.
Caps hit: open Cognito signup with no authorization on mutating routes (cap 69); secrets in plaintext Lambda env.

Scope: read-only. Code read on origin/main (HEAD 110b255), live AWS checks read-only (acct 434996132483, ap-south-1). No fixes made.

## Architecture (as found)
- EventBridge cron -> Python 3.12 Lambdas (Serverless v3) -> Postgres `khanal-db-prod` (shared, public, t4g.micro, single-AZ) + SAP B1 Service Layer (public IP) + SAP HANA reads + UC REST / Amazon SP-API.
- Staging state machines: UC `pipeline_status` fetched->ready->posting->posted (+batch_hold/failed/dead/verify/period_locked); Amazon `stage` fetched->ready->posting->posted (+failed/period_locked).
- Idempotency: atomic claim UPDATEs, SAP duplicate guards (Amazon NumAtCard+csNo; UC HANA INV1.U_Order, SL Comments fallback), period locks, posting cutoff.
- Dashboard: HTTP API + per-service Cognito JWT authorizer; live UI is Lovable order-post-visor (repo `dashboard/` is dead).
- Deploy: manual GH Actions workflow_dispatch (OIDC), pytest in CI.

## Blockers
1. **Open Cognito self-signup** (`AllowAdminCreateUserOnly: False`, MFA off) on all 4 pools (amazon/uc x prod/dev). Authorizer only checks a valid JWT; no role/group check. Anyone signing up can lock/unlock periods, retry/post invoices+CNs, Amazon `sync-orders?mode=seed_watermark` (skips unsynced orders), read order PII. Fix: `AdminCreateUserConfig.AllowAdminCreateUserOnly: true` now; then add group check in handlers (lock/unlock, retries, sync modes admin-only). Files: both `serverless.yml`, UC `handlers.py dashboard_handler`, Amazon `handlers.py`.
2. **No alerting on UC prod**: `UC_ALERT_ADMIN_EMAIL`/`UC_ALERT_FROM_EMAIL` EMPTY (logs: "Alert email not configured; skipping: pogrn crashed"). Zero CloudWatch alarms for either service, no DLQs. Failures are silent.
3. **SAP TLS verify off in prod** (`SAP_SL_VERIFY_SSL=false` on both) over public internet; Amazon yml comment claims verified. SAP creds + data MITM-able. Fix: pin SAP CA cert / proper cert, set true.

## High-value fixes
- Dashboard-Lambda bulk posting: Amazon `retry-invoices`/`retry-cn`/`post-ready` run SAP posts inline in 30s Lambda with unbounded `limit` -> killed mid-post, stuck `posting` (reclaimed after 30 min). Route via async invoke + cap limit.
- Remove debug/one-off Lambdas from prod stack (UC: 8, e.g. `AdminDebugGetInvoice` logs 1.6MB invoice PII, no log retention). Dead `debug-*` routes in Amazon handlers (`debug-pg-identity` leaks DB host). Set log retention.
- UC returns loss: `uc_client._search_returns_single` returns `[]` on non-OK and per-code failures are swallowed, then returns cursor advances -> returns permanently skipped. Fail the run / hold cursor.
- UC null `payload_json`: guard/verify-recovered posts call `mark_posted(..., None)`; CNs depend on parent payload_json (`returns.prepare_credit_note_items`) -> those CNs unresolved forever. Persist payload or rebuild from SAP doc.
- UC CN dedup keyed on base invoice only (`credit_notes.find_credit_note_by_base_invoice`): 2nd partial return may be marked posted with 1st CN's DocEntry. Verify with data.
- UC `pogrn`: non-idempotent multi-step UC writes; rows stuck `processing` after timeout never recovered.
- UC ship state missing -> silently defaults to KA (`sap_payload`), wrong CGST/SGST vs IGST. Refuse instead.
- UC CN `DocDate` falls back to `date.today()` and uses UTC `extract_return_date` (IST helper in `dates.py` unused).
- UC SL fallback `contains(Comments)` ignores cancelled invoices and can substring-collide.
- UC `U_Order_JSON` (up to 64KB, customer PII) copied onto every SAP line, truncated into invalid JSON.
- UC scheduled functions: default 2 async retries (Amazon has 0). Set `maximumRetryAttempts: 0`.
- UC HANA outage: `bulk_batch_inventory_by_warehouse` returns `[]` -> false "no batch" shortage -> batch_hold -> dead after 48h. Raise like Amazon does (HanaUnavailable -> revert to ready).
- Env clobber: deploy.yml passes unset secrets as empty strings (`UC_POSTING_CUTOFF_DATE` is `""` in prod; safe only via code default). Same class as HANA_USE_SSM incident. Fail deploy on missing secrets; use `npm ci`; add prod approval + concurrency group.
- `upsert_order` TOCTOU in both stores (ON CONFLICT resets stage unconditionally). Amazon self-heals via NumAtCard guard; UC relies on HANA/SL guard.
- Shared RDS: public 0.0.0.0/0:5432 (load-bearing, no VPC Lambdas), t4g.micro, FreeableMemory min 0.08GB, 52 peak conns, no pooling/RDS Proxy. Plan VPC + RDS Proxy.
- Money in floats; UC `recon.py` rebuilds payload from current code, doesn't verify DocEntry identity (U_Order/CANCELED), so DocEntry-corruption class could recur undetected.
- Amazon: `recon-download` requires `recon/` prefix but exports write `recon-export/`, `inventory-export/` (verify with frontend). CN amendment path does one SAP call per posted refund in 45d window every 6h. `constants.py` cutoff default (2026-06-01) differs from yml (07-01) and live (08-01): single source needed.
- Amazon `finances_api`: shipping-only refunds invisible (documented, MTR recon is the net); refund_id = order+PostedDate, so two refunds same timestamp merge.
- Legacy cruft: Frappe-parity "KNOWN BUG" qty-0 handling in `mappers.eligible_line_items`, Flex leftovers (`store.py`, `dashboard_queries.py`, `recon_summary.py`, `scripts/create_flex_cn_log_table.sql`), stale `DEBUG_FUNCTIONS=[]`, dead `dashboard/` + S3/CloudFront in UC stack.
- God-handlers: UC/Amazon `post_invoices_handler` carry ~25 one-off ops modes incl. irreversible purge/delete. Move to scripts.
- Errors return `str(exc)` to clients (UC dashboard).
- Single shared IAM role per service, `ses:SendEmail` on `*`; secrets in plaintext Lambda env (move to SSM/Secrets Manager).

## Strengths (short)
Atomic claims, pre-post SAP duplicate guards, period locks, cutoffs, FEFO batch + Sales BOM handling, SP-API backoff/breaker/advisory locks, DSN scrubbing, parameterized SQL, whitelisted ORDER BY, 14d RDS backups + deletion protection, tests for payload builders, CI green.

## Evidence checked
Code: both services' serverless.yml, handlers, all domain modules (UC fully; Amazon except report_parser/reports_api/recon/fba_ship_sync/dashboard_queries/alerts), deploy.yml, ci.yml.
Live (read-only): Cognito pool configs, Lambda env (booleans only), EventBridge schedules, CloudWatch alarms (1 total, none relevant), 7d Lambda error/throttle metrics (UC PoGrn 6/340 errs; Amazon PostCreditNotes 7/43, SP-API throttling; 0 throttles), RDS metrics (14d), S3 recon bucket config, log groups.
CI: `gh run list` — last CI + Deploy on main green (2026-10-22).

## Evidence missing
- Not read: Amazon `recon.py`, `reports_api.py`, `fba_ship_sync.py`, `dashboard_queries.py`, `report_parser.py`; `shared/khanal_shared`, layer build, `sql/*.sql`, `scripts/`.
- Tests: Amazon has no tests for `post_invoices`, `store`, `sap_client`, `handlers`, `hana_access`; UC has no dashboard auth/authorization test. Coverage % not measured; pytest not run locally.
- Not verified with prod data: multi-return CN collision, count of NULL `payload_json` posted orders, frontend use of recon-download.
- IAM inline policy contents not inspected. No restore drill of RDS backup.

## Next action
Today: set `AllowAdminCreateUserOnly: true` on all four Cognito pools (yml + deploy via GH Actions), set UC alert email env, then add admin-group check to mutating routes.
