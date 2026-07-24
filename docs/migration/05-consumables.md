# Phase 3a — consumables → Lambda + Postgres

This is a different kind of migration than Phases 0–2. Unicommerce/B2B/
Amazon US are **background workers** with no permission model and no
live end users clicking through screens. consumables (and, in the next
phase, real_aquatics and wowpets) is a **full application**: doctypes
with fields and validation, role-based permissions, an approval workflow,
and a live SPA frontend that real people use. This phase is the template
for that harder kind of migration — get it right here before repeating it
for the two apps with heavier daily usage.

## Why consumables first

You confirmed real_aquatics and wowpets have daily, live end users and
need careful handling; consumables is lower external-user exposure, so
it's the safer place to prove the "Frappe app → Lambda + Postgres"
pattern before the higher-stakes ones.

## Source (Frappe, verified)

`khanal_tech_integrations/consumables/`:

- **API**: `api/admin.py`, `api/auth.py`, `api/delivery_notes.py`,
  `api/inventory.py`, `api/item_requests.py`, `api/material_requests.py`,
  `api/notifications.py`, `api/procurement.py`, `api/transfers.py`
- **Doctypes** (each with its own permission rules + JSON schema):
  `consumables_cmr_purchase_link`, `consumables_cmr_transfer_link`,
  `consumables_delivery_note` (+ `_item`),
  `consumables_item_group`, `consumables_item_request`,
  `consumables_material_request` (+ `_item`), `consumables_plant`,
  `consumables_plant_warehouse`, `consumables_purchase_request` (+
  `_item`), `consumables_settings`, `consumables_transfer_request` (+
  `_item`), `consumables_user_plant`
- **Business logic**: `hana/client.py`, `hana/inventory_queries.py`,
  `hana/procurement_queries.py`, `services/fefo.py` (FEFO = first-expiry-
  first-out stock logic), `services/role_guard.py` (permission
  enforcement), `services/sap_delivery.py`, `services/sap_transfer.py`
- **Frontend**: `consumables-frontend/` — already a standalone Vue SPA
  with its own auth (`src/core/auth/index.js`), API layer
  (`src/core/api/*.js`), and pages for admin, delivery, inventory,
  material_requests, procurement, transfers. This talks to Frappe's REST/
  whitelisted-method API today; it does not need a rewrite.

## Architecture decision

The doctypes encode three things that need explicit re-homes, not just a
1:1 table copy:

1. **Schema** (fields, types) → Postgres tables, one per doctype family
   (`consumables.plants`, `consumables.material_requests` +
   `consumables.material_request_items`, etc.) — straightforward, this
   part is mechanical.
2. **Permissions** (`consumables_user_plant` links users to plants;
   `services/role_guard.py` enforces who can act on what) → becomes
   explicit authorization checks in each Lambda handler, backed by a
   Postgres table (`consumables.user_plant_roles`) instead of Frappe's
   role/permission engine. This is the part with no free lunch — Frappe's
   permission engine is generic; the Lambda API has to reimplement
   exactly the checks `role_guard.py` does today, not a simplified
   version, or a plant manager could end up seeing another plant's data.
3. **Workflow state** (material request → approval → procurement →
   delivery → transfer, per the page structure: `material_requests/New`
   → `Detail`, `procurement/Detail`, `transfers/New` → `Detail`,
   `delivery/New` → `Detail`) → an explicit state machine in application
   code (a `status` column + a small set of allowed transitions per
   table), not Frappe's generic workflow engine.

**Recommended shape:**

- New stack: `services/consumables/`
- HTTP API + Cognito (same pattern as every dashboard service), routes
  mirroring the existing frontend's API calls 1:1 initially
  (`api/admin.js` → `/v1/consumables/admin/*`, `api/inventory.js` →
  `/v1/consumables/inventory/*`, etc.) so `consumables-frontend` only
  needs a base-URL + auth-token swap, not a rewrite.
- `khanal_shared` gains the HANA client reuse (already exists for
  Amazon US/Walmart's read-only HANA access — confirm
  `consumables/hana/client.py`'s connection profile can reuse the shared
  one rather than needing a separate HANA credential set).
- FEFO logic (`services/fefo.py`) and SAP delivery/transfer posting
  (`services/sap_delivery.py`, `services/sap_transfer.py`) port over
  close to as-is — these are business logic, not Frappe-specific, so
  the actual algorithm doesn't need to change, only its hosting.

## Infrastructure changes

- No VPC.
- IAM: HANA read SSM params (reuse existing `/sap/inc/hana_*` family if
  the same HANA instance/schema is used; new params if consumables reads
  a different SAP company/schema), SAP SL params for delivery/transfer
  posting, Cognito for auth.
- Postgres: new `consumables` schema in the shared RDS instance — twelve-
  ish tables based on the doctype list above. This is the first phase
  where the new schema is large enough to warrant an actual migration
  tool; start using Alembic here (mentioned as a recommendation in
  `docs/DATABASE.md` but not yet adopted anywhere in this repo) rather
  than hand-written DDL, since consumables' schema will need real
  iteration.
- Auth: `consumables-frontend`'s `src/core/auth/index.js` currently talks
  to Frappe's session/login — this becomes Cognito-based auth, which is
  the one actual frontend code change needed (auth flow), everything
  else in the frontend can stay.

## Cutover runbook

1. Stand up the Postgres schema + Alembic migrations, no Lambda traffic
   yet — validate schema against real doctype data exported from Frappe
   (a one-time backfill script, not an ongoing sync).
2. Build the Lambda API with **read-only** endpoints first (inventory
   views, request listing) — deploy behind a **separate URL/stage**,
   don't touch the production frontend yet. Validate read paths return
   the same data Frappe's API does, side by side.
3. Add write endpoints (material request creation, approvals, transfers,
   deliveries) one workflow at a time, each validated against
   `role_guard.py`'s actual permission rules — write a test for every
   permission check that exists in Frappe today before considering that
   endpoint done, since this is the part most likely to silently regress.
4. Backfill/sync a window of live data from Frappe to Postgres (however
   long the cutover validation period needs), so the two systems can be
   compared on real, current data without yet being the system users
   touch.
5. Pick a low-traffic window, repoint `consumables-frontend`'s API base
   URL + auth at the new Lambda API for **all users at once** (unlike
   Phases 0–2, you can't dual-run a live CRUD app with two sources of
   truth without building explicit dual-write — for a first template
   migration, a clean cutover with a tested rollback is simpler than
   building dual-write infrastructure you'll throw away).
6. Keep Frappe's consumables app installed and untouched (not deleted)
   for a defined rollback window — if something's wrong, repoint the
   frontend back.
7. After the rollback window passes cleanly, decommission the Frappe
   consumables app (but not the whole Frappe instance — real_aquatics and
   wowpets are still on it).

## Risks

- **Permission logic gaps** are the highest risk in this entire phase —
  budget real test-writing time against `role_guard.py`'s actual rules,
  not just the happy path.
- **Clean-cutover-not-dual-run** means a bad deploy affects all
  consumables users at once — mitigate with a tested rollback (revert the
  frontend's base URL) rather than trying to build parallel-write
  infrastructure for what's meant to be the *template* run.
- Frontend auth-flow change (Frappe session → Cognito) is the one
  frontend code change in this phase — test it thoroughly since every
  other phase after this one repeats it.

## Cost estimate (incremental, `ap-south-1`)

| Item | Estimate | Notes |
|---|---|---|
| Lambda (HTTP API handler, on-demand not scheduled) | $0–3/mo | Internal plant-staff usage, likely low thousands of requests/mo |
| API Gateway HTTP API | <$1/mo | $1/1M requests |
| Cognito | $0/mo | Essentials tier, free to 10,000 MAU — well above plant-staff headcount |
| Postgres schema addition (12+ tables) | $0 incremental, **watch storage/connections** | First schema in this migration large enough that it's worth checking current RDS instance's connection limit and storage headroom before adding it |
| SES (notifications) | <$1/mo | `api/notifications.py`'s email volume, likely low |

**Total: roughly $1–5/month incremental**, plus the one-time engineering
cost of reimplementing permissions and workflow state, which is the real
cost of this phase.
