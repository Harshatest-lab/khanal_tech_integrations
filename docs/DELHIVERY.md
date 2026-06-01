# Delhivery LRN integration (Frappe + PostgreSQL + Lambda webhooks)

**Frappe** calls Delhivery’s track API and **writes the snapshot directly to PostgreSQL** (`delhivery_lr`). This repo’s **delivery-api** stack exposes only **Delhivery webhooks** (API Gateway REST + SQS + one worker Lambda): shipment status and POD.

## Architecture (async, no Lambda on the HTTP ack path)

1. **API Gateway (REST, regional)** — `POST` on each route integrates **directly** with **SQS `SendMessage`**. The client may send **`X-Delhivery-Webhook-Secret`** (optional method header, forwarded on the integration request so VTL can read it). Mapping templates URL-encode the JSON body and always set SQS message attributes **`webhookKind`** and **`webhookSecret`** (empty string if the header is absent). Response is fixed **`{"ok": true}`**. **No ingest Lambda** on the HTTP path.
2. **`DelhiveryWebhookQueue`** — buffers webhook payloads; failed processing retries then **DLQ** after 5 receives.
3. **`delhiveryWebhookWorker`** (SQS trigger, **VPC** + shared layer) — reads **raw Delhivery JSON** from the message body and **`webhookKind`** from message attributes; validates **`X-Delhivery-Webhook-Secret`** when **`DELHIVERY_WEBHOOK_SECRET`** is set and **`DELHIVERY_SKIP_WEBHOOK_SECRET`** is not enabled (via synthetic headers from **`webhookSecret`** attribute when present). Same DB logic as before: `delhivery_lr` / `delhivery_lr_status_events` / `delhivery_lr_pod`.

Delhivery’s HTTP wait time no longer includes any Lambda cold start on receive (only API Gateway + SQS).

## Endpoints (after `delivery-api` deploy)

| Method | Path | Caller | Purpose |
|--------|------|--------|---------|
| `POST` | `/v1/webhooks/delhivery/shipment-status` | **Delhivery** | Enqueue payload; worker updates `delhivery_lr` + `delhivery_lr_status_events` |
| `POST` | `/v1/webhooks/delhivery/pod` | **Delhivery** | Enqueue payload; worker appends `delhivery_lr_pod` |

**Base URL** is the **REST API** invoke URL for your stage (not the old HTTP API v2 URL). After deploy, copy it from the CloudFormation stack output **`DelhiveryWebhookRestApiUrl`** or run `cd services/delivery-api && npx serverless info --verbose` and use the REST invoke URL shown for this stack.

There is **no** `POST /v1/integrations/frappe/delhivery-tracking-snapshot` in this service anymore.

## Temporary dev: skip webhook secret (no `X-Delhivery-Webhook-Secret`)

For local/dev debugging when API Gateway cannot attach **`webhookSecret`** to SQS:

1. Set Lambda env **`DELHIVERY_SKIP_WEBHOOK_SECRET`** to **`true`** (deploy with `DELHIVERY_SKIP_WEBHOOK_SECRET=true` in CI or edit the function in the console).
2. **`curl`** without the `X-Delhivery-Webhook-Secret` header is enough; the worker will still write to RDS if DB connectivity works.

**Do not** enable this on a **public** production URL: anyone who can `POST` to your webhook can enqueue payloads. Turn **`DELHIVERY_SKIP_WEBHOOK_SECRET`** off and keep **`DELHIVERY_WEBHOOK_SECRET`** aligned with Delhivery’s **`X-Delhivery-Webhook-Secret`** before prod. The REST API does **not** require that header for `POST` (missing header yields an empty **`webhookSecret`** attribute; the worker rejects unless skip is enabled or the secret matches).

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

If Lambda env **`DELHIVERY_WEBHOOK_SECRET`** is set, the worker expects the same value in SQS message attribute **`webhookSecret`**, which API Gateway copies from the **`X-Delhivery-Webhook-Secret`** header when Delhivery sends it. If the header is absent, that attribute is omitted and the worker treats the call as unauthorized when a secret is configured.

(Only if you can configure Delhivery to send that header; otherwise leave **`DELHIVERY_WEBHOOK_SECRET`** unset until they support it.)

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
- **`DELHIVERY_WEBHOOK_SECRET`** — optional; verified in the **worker** using message attributes (see above).

`serverless.yml` passes `DATABASE_URL` and `DELHIVERY_WEBHOOK_SECRET` from the shell environment at deploy time (`${env:...}`). Set them in GitHub Actions / your deploy shell.

## After deploy — checklist in AWS / Delhivery

1. **Deploy** the stack (`build_shared_layer.sh`, then `npx serverless deploy` from `services/delivery-api`) with valid AWS credentials.
2. In **CloudFormation** → stack **`delivery-api-<stage>`** → **Outputs**, copy **`DelhiveryWebhookRestApiUrl`** (or equivalent from `serverless info`).
3. In **Delhivery** (or your integration admin), set webhook URLs to:
   - `{DelhiveryWebhookRestApiUrl}/v1/webhooks/delhivery/shipment-status`
   - `{DelhiveryWebhookRestApiUrl}/v1/webhooks/delhivery/pod`
4. **Retire** any old **HTTP API (v2)** invoke URLs so traffic is not split across two APIs.
5. In **CloudWatch**, confirm **`delhiveryWebhookWorker`** invocations and logs; watch **`DelhiveryWebhookDlq`** for poison messages.
6. Optionally enable **API Gateway execution access logging** or **X-Ray** on the stage (tracing is enabled in the template) for latency debugging.

## Troubleshooting: console Test = 200 but `curl` = 500

**Your `curl` is fine** if it matches:

```bash
curl -sS -i -X POST \
  'https://{restApiId}.execute-api.us-east-1.amazonaws.com/dev/v1/webhooks/delhivery/shipment-status' \
  -H 'Content-Type: application/json' \
  -H 'X-Delhivery-Webhook-Secret: {secret}' \
  --data-binary '{"lrnum":"...","status":"Completed",...}'
```

API Gateway **Test** runs against the **latest saved API definition**. The **invoke URL** (`curl`, Delhivery) runs whatever is **deployed to the stage** (`dev`). If those differ, Test can be 200 while `curl` returns `500 Internal server error`.

**Fix (pick one):**

1. **IaC (preferred):** redeploy so stage picks up the new deployment (check description `delivery-api-dev-sqs-v14` or later):

   ```bash
   cd services/delivery-api && npx serverless deploy --stage dev
   ```

2. **Console (immediate):** API Gateway → your REST API → **Resources** → **Actions** → **Deploy API** → stage **`dev`**.

3. **CLI (immediate, same as console deploy):**

   ```bash
   aws apigateway create-deployment \
     --rest-api-id x0rrkiom5b \
     --stage-name dev \
     --description 'manual deploy after mapping fix'
   ```

After deploy, repeat `curl` — it should return `200` and `{"ok":true}` like Test.

**Sanity checks:**

- `Content-Type: text/plain` → **415** (expected; only `application/json` is mapped).
- Stage **Deployments** tab: active deployment timestamp should match your last deploy.

## Security

- **Never** paste Delhivery **Bearer JWT** tokens into this repo or docs; Frappe should keep tokens in its own secrets store.
- Rotate any token that was exposed in chat or commits.
