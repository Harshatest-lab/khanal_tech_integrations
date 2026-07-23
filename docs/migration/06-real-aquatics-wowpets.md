# Phase 3b — real_aquatics, wowpets → Lambda + Postgres

**Both are live with daily end users today.** This is the highest-stakes
phase in the whole blueprint — do it only after Phase 3a (consumables)
has proven the Frappe-app-to-Lambda pattern works, including the
permission-reimplementation risk and the auth-flow cutover.

**Order between the two is not yet decided** — pick based on business
priority once Phase 3a is stable. Everything below applies to both; where
they differ structurally, it's called out.

## Source (Frappe, verified)

Both follow an almost identical module shape — same set of concerns,
separate implementations, not shared code:

`khanal_tech_integrations/api/real_aquatics/`: `auth.py`,
`dispatch_orders.py`, `inventory.py`, `kf_attachments.py`, `orders.py`,
`purchase_order.py`, `returns.py`, `sap_issue_for_production.py`,
`sap_log.py`, `sap_master.py`, `tickets.py`, `work_order.py`.

`khanal_tech_integrations/api/wowpets/`: same file list
(`auth.py`, `dispatch_orders.py`, `inventory.py`, `kf_attachments.py`,
`orders.py`, `purchase_order.py`, `returns.py`,
`sap_issue_for_production.py`, `sap_log.py`, `sap_master.py`,
`tickets.py`, `work_order.py`) — plus its own doctype pair
`wowpets_order`/`wowpets_order_item` and
`wowpets_purchase_order`/`wowpets_purchase_order_item`, and a README +
`WOWPETS_SAP_INTEGRATION_ISSUES.md` **worth reading before scoping** —
it documents known SAP integration issues specific to wowpets that
real_aquatics may not share.

Real-world usage: `docs/realaquatics_blueprint.md` and
`docs/realaquatics_production_deployment.md` already exist in the Frappe
repo — read these before starting, they may already contain design
decisions and production learnings for real_aquatics that shouldn't be
re-derived from scratch.

Frontends: `realaquatics-frontend/` (standalone) and wowpets' UI —
check whether wowpets has its own dedicated frontend directory or shares
`kiosk-frontend` (the tree search didn't turn up a `wowpets-frontend/`
directory distinct from `kiosk-frontend` — confirm which frontend
actually serves wowpets' end users before planning the frontend cutover).

## Architecture decision

Same three-part re-homing as consumables (schema → Postgres, permissions
→ application code, workflow → explicit state machine), with two added
concerns specific to these two apps:

1. **Order/dispatch/work-order flows touch SAP production and dispatch
   directly** (`sap_issue_for_production.py`, `work_order.py`,
   `dispatch_orders.py`) — these are closer to real-time operational
   flows than consumables' request/approval cycle. Get the SAP posting
   contract (what fields, what timing, what failure/retry semantics)
   fully understood before reimplementing, since a subtle behavior change
   here affects production floor operations, not just back-office data.
2. **`kf_attachments.py`** (attachment handling — likely Frappe's file
   storage) needs an explicit re-home to S3, with careful thought about
   any existing attachment URLs already shared with end users (e.g. in
   past tickets/emails) — a naive cutover breaks old links.

Because both are live daily, **dual-run is worth the extra effort here**
that it wasn't for consumables — these two justify building actual
parallel-run infrastructure (write to both Frappe's DB and the new
Postgres schema for a window, compare outputs) rather than a clean-cut
rollback plan, given the higher blast radius of getting it wrong. This is
a deliberate deviation from the consumables playbook, not an oversight —
consumables was the template precisely because it could absorb a clean-
cut approach; these two can't.

## Infrastructure changes

- Same shape as consumables: `services/real-aquatics/` and
  `services/wowpets/` as two separate stacks (not one shared stack,
  despite the near-identical file structure) — they're different
  businesses with different SAP company data even if the code shape
  rhymes; keep blast radius separated per the "one stack per domain"
  standard.
- Each gets its own Postgres schema (`real_aquatics`, `wowpets`), own
  Cognito pool, own IAM scoping to its own SAP/HANA SSM parameter family.
- **Do not** let the near-identical `api/real_aquatics/*` vs
  `api/wowpets/*` code tempt a shared Lambda layer module across the two
  — port each independently, since "SAP integration issues" documented
  for wowpets specifically suggests they've already diverged in ways that
  aren't visible from the file names alone.

## Cutover runbook (per app — repeat independently for each)

1. Read `docs/realaquatics_blueprint.md`,
   `docs/realaquatics_production_deployment.md`, and
   `api/wowpets/WOWPETS_SAP_INTEGRATION_ISSUES.md` fully before scoping
   either migration — these likely contain hard-won context that would
   otherwise get silently re-broken.
2. Same schema/permission/workflow re-homing steps as consumables
   (Phase 3a runbook steps 1–3), but budget more time for the
   production/dispatch SAP flows given their operational sensitivity.
3. Build **dual-write**: the Lambda API and Frappe both stay live,
   writing to their own stores, with a reconciliation job comparing
   order/dispatch/work-order state between the two on a schedule —
   this is new infrastructure Phase 3a didn't need, budget for it
   explicitly.
4. Run dual-write for a real business cycle (long enough to see the full
   order → dispatch → SAP-issue → return lifecycle happen at least once,
   not just a few days), reconciling continuously.
5. Cut the frontend over only after reconciliation shows zero
   discrepancies across a full cycle — and keep Frappe's write path live
   (not just installed, actually live) for an explicit rollback window
   even after the frontend switch, unlike consumables where Frappe could
   be left dormant immediately.
6. Decommission the Frappe app for this one only after the rollback
   window passes with no incidents.
7. Repeat for the second app.

## Risks

- **Production-floor impact**: `sap_issue_for_production.py` and
  `work_order.py` affect physical operations, not just data — a bug here
  has a real-world consequence beyond "wrong number in a dashboard."
  Treat this migration with proportionally more caution than consumables.
- **Attachment URL breakage**: `kf_attachments.py` migration needs an
  explicit plan for existing shared links, not just new uploads.
- **wowpets-specific SAP issues**: the existing
  `WOWPETS_SAP_INTEGRATION_ISSUES.md` doc suggests known quirks — read it
  before assuming wowpets' migration mirrors real_aquatics' 1:1.
- **Dual-write reconciliation complexity**: this is new engineering effort
  neither Phase 1 nor Phase 3a required — budget it as real scope, not an
  afterthought.

## Cost estimate (incremental, `ap-south-1`, per app — roughly doubles for both)

| Item | Estimate | Notes |
|---|---|---|
| Lambda (HTTP API handler) | $0–3/mo | Similar usage profile to consumables |
| API Gateway HTTP API | <$1/mo | $1/1M requests |
| Cognito | $0/mo | Essentials tier, free to 10,000 MAU |
| Postgres schema addition | $0 incremental | Watch cumulative storage/connections across all schemas added so far by this point in the migration |
| S3 (attachments) | <$2/mo | Depends on attachment volume/size — check current Frappe file storage size for `kf_attachments` to size this properly rather than guessing |
| Reconciliation job (dual-write comparison, scheduled Lambda) | <$1/mo | Low-frequency scheduled function, simple diff logic |

**Total: roughly $2–8/month incremental per app.** As with consumables,
the real cost here is engineering time — specifically the dual-write
reconciliation infrastructure and the extra caution around production-
floor flows — not AWS spend.
