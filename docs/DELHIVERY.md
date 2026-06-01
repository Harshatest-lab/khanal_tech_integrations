# Delhivery LRN integration (Frappe + PostgreSQL + Lambda webhooks)

**Frappe** calls Delhivery’s track API and **writes the snapshot directly to PostgreSQL** (`delhivery_lr`). This repo’s **delivery-api** stack exposes only **Delhivery webhooks** (API Gateway + two Lambdas + SQS): shipment status and POD.

## Architecture (async)

1. **`delhiveryWebhookIngest`** (HTTP API, **no VPC**) — validates optional `X-Delhivery-Webhook-Secret`, parses JSON, **`SendMessage`** to SQS, returns **`{"ok": true, "lrnum": "..."}`** quickly.
2. **`DelhiveryWebhookQueue`** — buffers webhook payloads; failed processing retries then **DLQ** after 5 receives.
3. **`delhiveryWebhookWorker`** (SQS trigger, **VPC** + shared layer) — same DB logic as before: `delhivery_lr` / `delhivery_lr_status_events` / `delhivery_lr_pod`.

Delhivery’s HTTP wait time no longer includes VPC cold start or Postgres round-trips.

## Endpoints (after `delivery-api` deploy)

| Method | Path | Caller | Purpose |
|--------|------|--------|---------|
| `POST` | `/v1/webhooks/delhivery/shipment-status` | **Delhivery** | Enqueue payload; worker updates `delhivery_lr` + `delhivery_lr_status_events` |
| `POST` | `/v1/webhooks/delhivery/pod` | **Delhivery** | Enqueue payload; worker appends `delhivery_lr_pod` |

Base URL = API Gateway invoke URL for the `delivery-api` stage (AWS console / `serverless info`). **URLs and paths are unchanged** from the old synchronous stack.

There is **no** `POST /v1/integrations/frappe/delhivery-tracking-snapshot` in this service anymore.

## Database

Run once per database (adjust schema if needed):

```bash
psql "$DATABASE_URL" -f sql/delhivery_lr.sql
```

Tables:

- **`delhivery_lr`** — one row per `lrnum`: latest `delhivery_status`, optional `status_type`, last webhook payload, full **track** response in `raw_tracking` (**from Frappe**, not Lambda).
- **`delhivery_lr_status_events`** — append-only audit of each shipment-status webhook.
- **`delhivery_lr_pod`** — append-only POD pushes (revised PODs = multiple rows per `lrnum`).

## Frappe → PostgreSQL (tracking snapshot)

After your server-side Delhivery GET succeeds, **upsert** into `delhivery_lr` from Frappe using the same semantics the old Lambda used:

- **`lrnum`** — primary key (from `delhivery_track_response.data.lrnum` or your Doc).
- **`delhivery_status`** — e.g. from `delhivery_track_response.data.status` when present.
- **`raw_tracking`** — full JSON body from Delhivery as `jsonb`.

Example SQL (parameterize in Frappe / frappe.db.sql):

```sql
INSERT INTO delhivery_lr (
  lrnum, delhivery_status, raw_tracking, updated_at, created_at
) VALUES (
  %(lrnum)s, %(delhivery_status)s, %(raw_tracking)s::jsonb, now(), now()
)
ON CONFLICT (lrnum) DO UPDATE SET
  delhivery_status = EXCLUDED.delhivery_status,
  raw_tracking = EXCLUDED.raw_tracking,
  updated_at = now();
```

Optional columns on `delhivery_lr` (e.g. `doc_entry`, `doc_num`, `series`, `sap_response`) can be set in the same `INSERT`/`UPDATE` from Frappe if you need them.

## Delhivery → Lambda (shipment status webhook)

Register URL: `POST …/v1/webhooks/delhivery/shipment-status`.

Example body (fields may vary):

```json
{
  "lrnum": "220256841",
  "status": "DELIVERED",
  "status_type": "DL",
  "shipment_remark": "Shipment delivered successfully",
  "location": "Bangalore Hub",
  "mwn": "85334610000033",
  "timestamp": 1779381000
}
```

- **`delhivery_status`** in DB is driven from **`status`** (e.g. `Manifested`, `DELIVERED`).
- If **`status_type`** is present in the payload, it is stored (e.g. `UD`, `DL`, `RT`, `LT` per Delhivery docs).

**Optional shared secret**

If Lambda env **`DELHIVERY_WEBHOOK_SECRET`** is set, requests must include:

- `X-Delhivery-Webhook-Secret: <same value>`

(Only if you can configure Delhivery to send that header; otherwise leave unset until they support it.)

## Delhivery → Lambda (POD webhook)

Register URL: `POST …/v1/webhooks/delhivery/pod`.

Example body:

```json
{
  "waybill": "1234567890",
  "EPOD": "<base64>",
  "orderID": "ORD-99",
  "lrnum": "220256789"
}
```

Each push appends one row to `delhivery_lr_pod` (revised PODs = multiple rows).

## Lambda configuration

- **`DATABASE_URL`** — PostgreSQL DSN (RDS / RDS Proxy recommended).
- **`DELHIVERY_WEBHOOK_SECRET`** — optional; verifies webhook calls.

`serverless.yml` passes `DATABASE_URL` and `DELHIVERY_WEBHOOK_SECRET` from the shell environment at deploy time (`${env:...}`). Set them in GitHub Actions / your deploy shell.

## Security

- **Never** paste Delhivery **Bearer JWT** tokens into this repo or docs; Frappe should keep tokens in its own secrets store.
- Rotate any token that was exposed in chat or commits.
