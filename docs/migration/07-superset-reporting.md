# Phase 4 — Apache Superset reporting

Independent of every other phase — only needs read access to RDS
Postgres and SAP HANA, both of which already exist today. Can start in
parallel with Phase 0/1/2/3 work, or be sequenced wherever makes sense
given team bandwidth.

## Architecture decision

**No ETL layer** — confirmed decision: Superset queries RDS Postgres and
SAP HANA directly via its native SQLAlchemy connectors. Cheapest option
at current data volume; revisit only if dashboard query load starts
measurably straining either source.

**Hosting: small EC2/Lightsail instance running Superset's official
Docker Compose bundle**, not ECS Fargate. Reasoning:

- Superset has no official serverless/Fargate-optimized packaging — its
  reference deployment is Docker Compose (web + worker + Celery beat +
  Redis + its own metadata Postgres), which maps directly onto a single
  small VM.
- Fargate would mean running the same multi-container bundle as separate
  tasks/services, plus a load balancer for the web container if you want
  it reachable — more moving parts and a higher baseline cost (ALB alone
  runs ~$16-20/mo before any compute) for no real benefit at this scale.
- A single VM is the cheapest fixed cost and the standard way this
  specific tool is deployed in practice.
- Revisit Fargate only if uptime/scaling requirements grow beyond what a
  single small instance with basic monitoring can handle — not a
  decision to make preemptively.

**Instance sizing**: Superset's own Docker Compose bundle (web + worker +
beat + Redis + metadata Postgres, all on one box) realistically needs at
least 2GB RAM to run all containers without swapping, more comfortably
4GB. Lightsail's Linux bundle pricing runs roughly $5–$20/month across
its small-to-medium tiers, but **the exact bundle-to-RAM/vCPU mapping
should be confirmed in the Lightsail console at deploy time** — it
wasn't possible to pin down an authoritative ap-south-1-specific number
from public pricing pages during this planning pass, and bundle specs
have been known to change. Don't commit to a specific bundle name in this
doc; size it against actual container memory usage once Superset is
running.

## Infrastructure changes

- One Lightsail instance (or EC2 t-series if Lightsail's bundles don't
  fit — compare both at deploy time), `ap-south-1`, running Superset's
  Docker Compose stack.
- Two new **read-only** database users:
  - Postgres: a read-only role on the shared RDS instance, scoped to the
    schemas Superset actually needs (start narrow — grant schema access
    incrementally as dashboards are built, not a blanket read-all grant).
  - HANA: a read-only user for the SAP HANA connection, mirroring the
    read-only pattern `amazon-us-b2c` already uses for its own HANA
    access (`/sap/inc/hana_config`, `/sap/inc/hana_credentials` — check
    whether Superset should get its own dedicated HANA user rather than
    reusing Lambda's, since a compromised Superset box shouldn't have the
    same blast radius as a Lambda function's tightly-scoped IAM role).
- Security group: inbound 443 (Superset UI) from wherever staff actually
  need access (office IP range or a VPN, not `0.0.0.0/0` — this is a
  reporting tool with real business data, not a dev sandbox), inbound
  5432/HANA-port outbound rules to reach RDS and HANA.
- No VPC requirement beyond what RDS/HANA already need for connectivity —
  same public-endpoint-plus-SG-allowlist pattern as the Lambda side, just
  from a different compute source (a VM instead of a Lambda ENI).
- TLS: put Superset behind its own cert (Let's Encrypt via Caddy/nginx
  in front of the Compose stack, or Lightsail's own load balancer add-on
  if the extra ~$18/mo is acceptable for managed cert renewal).

## Rollout

1. Stand up the instance, run Superset's Docker Compose bundle, confirm
   it comes up cleanly and the default admin account is secured
   immediately (change default credentials before opening the SG at all).
2. Add the two read-only DB connections (Postgres, HANA).
3. Build a first dashboard against whichever data source is most
   immediately useful (this is independent of the Frappe migration
   phases — Postgres already has data from any Lambda service already
   live, e.g. `amazon-us-b2c`'s tables, even before Unicommerce/B2B/
   consumables land).
4. Add dashboards incrementally as each migration phase lands its own
   Postgres schema — Superset's utility grows automatically as Phases
   1–3 progress, no extra Superset-side work required per phase beyond
   building the actual dashboard.
5. Set up basic instance monitoring (CloudWatch agent or even just
   uptime checks) — this is a single VM with no auto-recovery by
   default, unlike Lambda; decide what an acceptable downtime window is
   for a reporting tool (probably generous, since it's not
   transaction-critical) and monitor accordingly.

## Risks

- Single VM = single point of failure for reporting (acceptable for a
  non-transactional reporting tool, but worth stating explicitly rather
  than assuming).
- Read-only DB users still need real scoping discipline — "read-only"
  isn't the same as "least privilege"; grant per-schema, not instance-wide.
- HANA connection pool limits: SAP HANA licenses often cap concurrent
  connections — check the current HANA connection budget before adding
  Superset's connection pool on top of whatever Lambda services already
  use, so Superset doesn't starve a production integration of a HANA
  connection slot.

## Cost estimate (incremental, `ap-south-1`)

| Item | Estimate | Notes |
|---|---|---|
| Lightsail instance (2-4GB RAM bundle) | $10–20/mo | **Confirm exact bundle price in console before committing** — see note above |
| TLS/reverse proxy | $0 | Let's Encrypt via Caddy/nginx on the same box, no extra cost |
| Data transfer (Superset querying RDS/HANA) | <$1/mo | Same-region traffic, low volume at dashboard-query scale |
| Read-only Postgres user | $0 | No incremental RDS cost, existing instance |
| Read-only HANA user | $0 (AWS side) | Check whether SAP licensing has any per-user cost — that's outside AWS billing and wasn't verified here |

**Total: roughly $10–21/month**, the single largest new recurring AWS
cost line in this entire blueprint — everything else across Phases 0–3
stays in the free tier or low single dollars.
