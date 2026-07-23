# Frappe → Lambda migration blueprint — overview

**Status:** planning. No cutover has started as of 2026-07-23.

This is the index for the full migration blueprint. Each phase has its own
doc with architecture decisions, infrastructure, a cutover runbook, risks,
and a cost estimate. Read this file first for the shape of the whole thing.

## Why

Two GitHub repos are in play:

- **`khanal_tech_integrations`** — a Frappe app running on an EC2 box. Holds
  every integration (Unicommerce, B2B PO automation, SafeExpress, DSR,
  Amazon US legacy) plus three full line-of-business apps (**wowpets**,
  **real_aquatics**, **consumables**) with their own doctypes, workflows,
  and SPA frontends (`kiosk-frontend`, `realaquatics-frontend`,
  `consumables-frontend`, `amazonus-frontend`).
- **`Khanal_tech_serverless`** (this repo) — Serverless Framework v3,
  Python 3.12 Lambda stacks, one per business domain. Already runs
  `delivery-api`, `dogseeus-invoice`, `B2C_Invoice`, `amazon-us-b2c`.

Target: zero Frappe, zero Frappe EC2. Every integration and app becomes a
Lambda stack in this repo, Postgres RDS is the system of record, SAP
B1/HANA stay the external ERP truth. Apache Superset queries Postgres +
HANA directly for reporting. No hard deadline — phase it safely, verify
each piece in parallel before cutting Frappe off.

## Current state (verified against the repos on 2026-07-23, not memory)

| Area | Where it lives today | Cadence |
|---|---|---|
| Unicommerce (fetch/post/GRN/inventory/recon) | `khanal_tech_integrations/unicommerce/workers/*` | 30-min offset crons |
| B2B PO automation — Bigbasket, Blink, Instamart, More, Zepto | `khanal_tech_integrations/utils/B2B/*` | 5-min cron, IMAP-based |
| B2B PO automation — Flipkart | `khanal_tech_integrations/utils/B2B/Flipkart.py`, `flipkart_SAP.py` | code exists, **not found in the cron list — confirm it's actually wired up** |
| SafeExpress (2 implementations) | `utils/safexpress`, `utils/safexpress2` | daily / every 3h |
| DSR Logistics | `utils/DSR_Logisticss/*` | every 4h / 6h retry |
| Amazon US | `khanal_tech_integrations/amazon_us/workers/*` **and** `services/amazon-us-b2c` (this repo) | Frappe side live in prod; Lambda side deployed with **test DB creds, schedules `enabled: false`** — half-finished cutover, not yet a conflict but a loaded gun |
| wowpets, real_aquatics | doctypes + `api/wowpets`, `api/real_aquatics` + `kiosk-frontend`/`realaquatics-frontend` | **live, daily end users** |
| consumables | doctypes + `consumables/*` + `consumables-frontend` | live, lower external-user exposure than the two above |

## Phase order

| Phase | What | Doc |
|---|---|---|
| 0 | Delete SafeExpress + DSR (business stopped, confirmed) | [01-decommission-safexpress-dsr.md](01-decommission-safexpress-dsr.md) |
| 0.5 | Finish the Amazon US cutover that's already half-built | [02-amazon-us-cutover.md](02-amazon-us-cutover.md) |
| 1 | Unicommerce → Lambda | [03-unicommerce-b2c.md](03-unicommerce-b2c.md) |
| 2 | B2B PO automation (6 marketplaces) → Lambda | [04-b2b-po-automation.md](04-b2b-po-automation.md) |
| 3a | consumables → Lambda + Postgres | [05-consumables.md](05-consumables.md) |
| 3b | real_aquatics, wowpets → Lambda + Postgres | [06-real-aquatics-wowpets.md](06-real-aquatics-wowpets.md) |
| 4 | Apache Superset reporting (parallel-able, no dependency on the above) | [07-superset-reporting.md](07-superset-reporting.md) |
| 5 | Decommission Frappe + EC2 | [08-frappe-decommission.md](08-frappe-decommission.md) |

Phase 4 has no dependency on 0–3 and can start any time — it only needs
read access to RDS Postgres and SAP HANA, both of which already exist.

## Standards every phase follows (already established in this repo — don't diverge)

- One `serverless.yml` stack per domain, not a monolith (matches the
  existing 4-service pattern).
- Shared code only in the `khanal_shared` Lambda layer, built by
  `scripts/build_shared_layer.sh` — no copy-pasted SAP/HANA/Postgres
  clients between services.
- **No Lambda VPC** unless a resource truly requires it. RDS is already
  public-endpoint + security-group-gated specifically to avoid NAT
  Gateway cost (`docs/DEV_MIGRATE_LAMBDA_OUT_OF_VPC.md` — saved
  ~$75–80/mo in dev by removing NAT + the SSM VPC endpoint). Every new
  service defaults to this; a `vpc:` block on a function is a deviation
  that needs a written reason.
- Secrets/config in SSM Parameter Store, least-privilege IAM scoped to
  exact parameter ARNs per function (see `amazon-us-b2c/serverless.yml`'s
  `iam.role.statements` — that granularity, not a blanket policy).
- GitHub Actions + OIDC role assumption, `workflow_dispatch` per
  service/stage, no long-lived AWS keys.
- One Cognito user pool per dashboard-facing service, not a shared auth
  server — keeps blast radius isolated per domain.
- Dual-run every cutover: new Lambda path runs and is verified against
  SAP/Postgres/the old dashboard for a defined window before the Frappe
  side is disabled, and before its code is deleted.

## Cost estimates — how to read them

Every phase doc has a cost table. AWS unit prices are pulled from public
July-2026 pricing for `ap-south-1`:

- **Lambda:** $0.20 / 1M requests + $0.0000166667 / GB-second (x86). Free
  tier: 1M requests + 400,000 GB-s per month, per account, forever (not
  just first year).
- **API Gateway HTTP API:** $1.00 / 1M requests.
- **Cognito:** Essentials tier, free up to 10,000 MAU/month.
- **SES:** $0.10 / 1,000 emails (no EC2-sourced free tier anymore; 3,000
  free messages/month for new accounts only).
- **RDS:** existing instance already costs ~$12/mo per
  `docs/DEV_MIGRATE_LAMBDA_OUT_OF_VPC.md` (Step 8 billing table) — this
  is a **sunk cost**, not incremental, unless a phase needs a bigger
  instance class or more storage.
- **Lightsail (Superset):** Linux plans run roughly $5–$20/mo depending
  on RAM/vCPU bundle. **The exact RAM-to-price mapping was not pinned
  down from search results and may have changed — confirm the current
  bundle spec in the Lightsail console before committing to a size.**
- **NAT Gateway:** $0 — not used, by design.

These are workload-scale estimates for **low-volume scheduled workers and
small internal dashboards** (thousands, not millions, of
requests/emails/invocations per month). At this scale nearly everything
in Phases 0–3 stays inside the Lambda/API-Gateway/Cognito free tiers —
the real incremental cost is Phase 4 (Superset compute) and any RDS
instance-class bump.

**Open item:** current Frappe EC2 instance type/size wasn't available
from the repos (it's infra, not code) — get that from the AWS console
before Phase 5 to compute the actual EC2 savings from decommissioning.

## Open items across all phases (resolve before/at the relevant phase)

1. Confirm Flipkart's B2B automation is actually scheduled somewhere (not
   found in `hooks.py`'s cron block) before assuming it's one of the six
   live marketplace flows.
2. Sequence between real_aquatics and wowpets in Phase 3b — both are live
   with daily end users; decide priority when Phase 3a (consumables) is
   proven out.
3. Current Frappe EC2 instance type/size, for the Phase 5 savings number.
