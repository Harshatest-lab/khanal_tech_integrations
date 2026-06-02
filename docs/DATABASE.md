# PostgreSQL, RDS Proxy, and shared data access

## RDS + RDS Proxy

**Why Proxy**

- Lambdas open many short-lived connections; Proxy pools toward the database.
- Supports **IAM authentication** (optional) and **Secrets Manager** integration.

**Connectivity**

- Lambdas in VPC **private subnets** with egress via NAT (for public APIs like Amazon SP-API / carriers) *or* VPC endpoints for AWS APIs to avoid NAT cost where possible.
- Security groups:
  - `sg-lambda` → egress 443 to internet or endpoints; egress 5432 to `sg-proxy` only.
  - `sg-proxy` → ingress 5432 from `sg-lambda`; egress 5432 to `sg-rds`.
  - `sg-rds` → ingress 5432 from `sg-proxy` only.

**Lambda settings**

- Set `RDS_PROXY_ENDPOINT` (or full DSN) via SSM.
- Keep **connection acquisition** lazy; reuse global connection pool object where your driver supports it (see `khanal_shared.db.pool`).

---

## Schemas (recommended)

| Schema | Contents |
|--------|----------|
| `core` | tenants, customers, products, orders |
| `fulfillment` | shipments, events |
| `integrations` | webhooks, jobs, external maps, call logs |
| `tax` | *(optional)* quotes/transactions if you add tax later |

---

## Reference tables (DDL-style)

> Use **Alembic** in production; this is a baseline for greenfield environments.

```sql
-- Tenancy
CREATE TABLE core.tenants (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- Orders
CREATE TABLE core.orders (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES core.tenants(id),
  channel          text NOT NULL, -- amazon, manual, other
  external_order_id text,
  status           text NOT NULL,
  currency         char(3) NOT NULL,
  subtotal_cents   bigint NOT NULL,
  tax_cents        bigint NOT NULL DEFAULT 0,
  total_cents      bigint NOT NULL,
  version          int NOT NULL DEFAULT 1, -- optimistic concurrency
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, channel, external_order_id)
);

CREATE TABLE core.order_lines (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id       uuid NOT NULL REFERENCES core.orders(id) ON DELETE CASCADE,
  sku            text NOT NULL,
  quantity       int NOT NULL,
  unit_price_cents bigint NOT NULL
);

-- Generic external id map (avoids dozens of mapping tables early)
CREATE TABLE integrations.external_entity_map (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES core.tenants(id),
  provider        text NOT NULL,
  entity_type     text NOT NULL, -- order, product, shipment
  external_id     text NOT NULL,
  internal_id     uuid NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, provider, entity_type, external_id)
);

-- Webhook durability + idempotency
CREATE TABLE integrations.webhook_inbox (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES core.tenants(id),
  provider         text NOT NULL,
  idempotency_key  text NOT NULL,
  headers          jsonb,
  body             jsonb NOT NULL,
  received_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, provider, idempotency_key)
);

CREATE TABLE integrations.integration_jobs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES core.tenants(id),
  job_type         text NOT NULL,
  payload          jsonb NOT NULL,
  status           text NOT NULL DEFAULT 'pending', -- pending, running, done, dead
  attempts         int NOT NULL DEFAULT 0,
  available_at     timestamptz NOT NULL DEFAULT now(),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE integrations.integration_call_log (
  id               bigserial PRIMARY KEY,
  tenant_id        uuid REFERENCES core.tenants(id),
  provider         text NOT NULL,
  operation        text NOT NULL,
  request_id       text,
  http_status      int,
  duration_ms      int,
  created_at       timestamptz NOT NULL DEFAULT now()
);

-- Optional later: CREATE SCHEMA tax; tax quotes / remittance tables if you add a tax engine.

-- Fulfillment
CREATE TABLE fulfillment.shipments (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES core.tenants(id),
  order_id         uuid REFERENCES core.orders(id),
  carrier          text NOT NULL,
  tracking_number  text,
  status           text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE fulfillment.shipment_events (
  id               bigserial PRIMARY KEY,
  shipment_id      uuid NOT NULL REFERENCES fulfillment.shipments(id) ON DELETE CASCADE,
  event_type       text NOT NULL,
  raw_payload      jsonb,
  occurred_at      timestamptz NOT NULL DEFAULT now()
);
```

---

## Indexes (add as queries emerge)

- `core.orders (tenant_id, status, created_at DESC)`
- `integrations.integration_jobs (status, available_at)` partial where `status = 'pending'`
- `fulfillment.shipments (tenant_id, tracking_number)` where tracking is searchable

---

## Shared DB access layer (Python)

See `shared/khanal_shared/db/`:

- `config.py` — DSN from env.
- `pool.py` — module-level pool (psycopg **v3** connection pool recommended for Lambda when configured with small `min_size` / `max_size`).

**Transaction pattern**

```python
with pool.connection() as conn:
    with conn.transaction():
        repo_orders.save(conn, ...)
        repo_jobs.enqueue(conn, ...)
```

---

## Migrations

- Add **Alembic** with `env.py` reading `DATABASE_URL` from env.
- CI: `alembic upgrade head` against ephemeral DB for validation; prod deploy runs migration **before** or **after** Lambda deploy depending on backward compatibility (prefer **expand** migrations compatible with old code).

Delhivery LRN tables (standalone DDL): [sql/delhivery_lr.sql](../sql/delhivery_lr.sql) — see [DELHIVERY.md](DELHIVERY.md). POD-related columns include **`delhivery_lr.latest_pod_id`**, **`delhivery_lr.pod_received`**, and on **`delhivery_lr_pod`**: **`pod_url`**, **`pod_format`** (plus **`epod_b64`**, **`raw_payload`**).
