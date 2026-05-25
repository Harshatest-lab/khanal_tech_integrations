# Delhivery LRN integration (Frappe + Lambda + PostgreSQL)

No Frappe UI or Delhivery **GET** calls live in this repo. **Frappe** calls Delhivery’s track API; this service **stores** the JSON and accepts **shipment status webhooks** from Delhivery.

## Endpoints (after `delivery-api` deploy)

| Method | Path | Caller | Purpose |
|--------|------|--------|---------|
| `POST` | `/v1/integrations/frappe/delhivery-tracking-snapshot` | **Frappe** (server-side) | Upsert full Delhivery **GET /lrn/track** JSON into Postgres |
| `POST` | `/v1/webhooks/delhivery/shipment-status` | **Delhivery** | Update `delhivery_status` / fields by `lrnum` when status changes (e.g. `DELIVERED`) |

Base URL = API Gateway invoke URL for the `delivery-api` stage (shown in AWS console / `serverless info`).

## Database

Run once per database (adjust schema if needed):

```bash
psql "$DATABASE_URL" -f sql/delhivery_lr.sql
```

Tables:

- **`delhivery_lr`** — one row per `lrnum`: latest `delhivery_status`, optional `status_type`, last webhook payload, full **track** response in `raw_tracking` (from Frappe).
- **`delhivery_lr_status_events`** — append-only audit of each shipment-status webhook.

## Frappe → Lambda (tracking snapshot)

After your server-side Delhivery GET succeeds, **POST** JSON:

```json
{
  "lrnum": "220256841",
  "delhivery_track_response": {
    "success": true,
    "request_id": "...",
    "data": {
      "lrnum": "220256841",
      "status": "MANIFESTED",
      "wbns": []
    }
  }
}
```

`lrnum` may be omitted if it appears under `delhivery_track_response.data.lrnum`.

**Headers**

- `X-Api-Key: <same value as Lambda env FRAPPE_INGEST_API_KEY>`

Set `FRAPPE_INGEST_API_KEY` when deploying (CI secret / shell env); never commit real keys.

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

## Lambda configuration

- **`DATABASE_URL`** — PostgreSQL DSN (RDS / RDS Proxy recommended).
- **`FRAPPE_INGEST_API_KEY`** — required for the Frappe ingest route.
- **`DELHIVERY_WEBHOOK_SECRET`** — optional; verifies webhook calls.

`serverless.yml` passes `FRAPPE_INGEST_API_KEY` and `DELHIVERY_WEBHOOK_SECRET` from the shell environment at deploy time (`${env:...}`). Set them in GitHub Actions / your deploy shell.

## Security

- **Never** paste Delhivery **Bearer JWT** tokens into this repo or docs; Frappe should keep tokens in its own secrets store.
- Rotate any token that was exposed in chat or commits.

## Next (POD webhook)

A second webhook (S3 URL / base64 EPOD) can follow the same pattern: new path, new handler, new table (e.g. `delhivery_lr_pod_events`) — not implemented in this pass.
