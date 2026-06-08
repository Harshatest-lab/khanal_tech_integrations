# Delhivery LRN integration (Frappe + PostgreSQL + Lambda webhooks)

**Greenfield AWS (VPC, RDS, IAM, endpoints):** see [INFRA_AWS_FROM_SCRATCH.md](./INFRA_AWS_FROM_SCRATCH.md) for a step-by-step build order before deploy.

**Frappe** calls Delhivery’s track API and **writes the snapshot directly to PostgreSQL** (`delhivery_lr`). This repo’s **delivery-api** stack exposes only **Delhivery webhooks** (API Gateway REST + SQS + one worker Lambda): shipment status and POD.

## Architecture (async, no Lambda on the HTTP ack path)

1. **API Gateway (REST, regional)** — `POST` on each route integrates **directly** with **SQS `SendMessage`**. The client may send **`X-Delhivery-Webhook-Secret`** (optional **`method.request`** header for VTL **`$input.params`**). Mapping templates URL-encode the JSON body, set **`webhookKind`**, and add **`webhookSecret`** only when the header is non-empty (SQS rejects empty string message attribute values). Response is fixed **`{"ok": true}`**. **No ingest Lambda** on the HTTP path.
2. **`DelhiveryWebhookQueue`** — buffers webhook payloads; failed processing retries then **DLQ** after 5 receives.
3. **`delhiveryWebhookWorker`** (SQS trigger, **VPC** + shared layer) — reads **raw Delhivery JSON** from the message body and **`webhookKind`** from message attributes; validates **`X-Delhivery-Webhook-Secret`** when **`DELHIVERY_WEBHOOK_SECRET`** is set and **`DELHIVERY_SKIP_WEBHOOK_SECRET`** is not enabled (via synthetic headers from **`webhookSecret`** attribute when present). Writes **`delhivery_lr`** + **`delhivery_lr_status_events`** for shipment status; for POD, optionally decodes base64 **EPOD** to **`POD_S3_BUCKET`**, then appends **`delhivery_lr_pod`**, sets **`delhivery_lr.latest_pod_id`** + **`pod_received`** when a matching **`delhivery_lr`** row exists, then optionally **PATCH**es SAP **Invoices** when SAP is enabled (**`SAP_SL_*`** env **or** **`SAP_SL_USE_SSM`**) and **`doc_entry`** is set (no dependency on **Delivered** status).

Delhivery’s HTTP wait time no longer includes any Lambda cold start on receive (only API Gateway + SQS).

## Endpoints (after `delivery-api` deploy)

| Method | Path | Caller | Purpose |
|--------|------|--------|---------|
| `POST` | `/v1/webhooks/delhivery/shipment-status` | **Delhivery** | Enqueue payload; worker updates `delhivery_lr` + `delhivery_lr_status_events` |
| `POST` | `/v1/webhooks/delhivery/pod` | **Delhivery** | Enqueue payload; worker appends **`delhivery_lr_pod`**, updates **`delhivery_lr.latest_pod_id`** + **`pod_received`** when LR exists |

**Base URL** is the **REST API** invoke URL for your stage (not the old HTTP API v2 URL). After deploy, copy it from the CloudFormation stack output **`DelhiveryWebhookRestApiUrl`** or run `cd services/delivery-api && npx serverless info --verbose` and use the REST invoke URL shown for this stack.

There is **no** `POST /v1/integrations/frappe/delhivery-tracking-snapshot` in this service anymore.

## Temporary dev: skip webhook secret (no `X-Delhivery-Webhook-Secret`)

For local/dev debugging when API Gateway cannot attach **`webhookSecret`** to SQS:

1. Set Lambda env **`DELHIVERY_SKIP_WEBHOOK_SECRET`** to **`true`** (deploy with `DELHIVERY_SKIP_WEBHOOK_SECRET=true` in CI or edit the function in the console).
2. **`curl`** without the `X-Delhivery-Webhook-Secret` header is enough; the worker will still write to RDS if DB connectivity works.

**Do not** enable this on a **public** production URL: anyone who can `POST` to your webhook can enqueue payloads. Turn **`DELHIVERY_SKIP_WEBHOOK_SECRET`** off and keep **`DELHIVERY_WEBHOOK_SECRET`** aligned with Delhivery’s **`X-Delhivery-Webhook-Secret`** before prod. If the header is omitted, **`webhookSecret`** is not sent on the SQS message (empty values are invalid for SQS); the worker treats missing secret like an unauthorized payload unless skip is enabled.

## Database

Run once per database (adjust schema if needed):

```bash
psql "$DATABASE_URL" -f sql/delhivery_lr.sql
```

Tables:

- **`delhivery_lr`** — one row per `lrnum`: latest `delhivery_status`, optional `status_type`, last webhook payload, full **track** response in `raw_tracking` (**from Frappe**, not Lambda). **`latest_pod_id`** points at the newest **`delhivery_lr_pod`** row; **`pod_received`** is set when at least one POD has been stored for that LR.
- **`delhivery_lr_status_events`** — append-only audit of each shipment-status webhook.
- **`delhivery_lr_pod`** — append-only POD history (re-triggers / revised PODs = multiple rows per `lrnum`). Columns include **`epod_b64`** (only when S3 upload is disabled), optional **`pod_url`**, optional **`pod_date`** (POD / delivery date from the webhook; mirrors SAP **`U_POD_DATE`** when synced), optional **`sap_pod_response`** (Lambda-only JSON: SAP Service Layer PATCH audit for this POD row — **not** the same as Frappe’s **`delhivery_lr.sap_response`**), and **`pod_format`** (`base64` \| `url` \| `mixed` \| `raw`). When **`POD_S3_BUCKET`** is configured, base64 **EPOD** is decoded in Lambda, written to S3, and the row stores **`pod_url`** with **`epod_b64`** left null (full webhook JSON remains in **`raw_payload`**). Inserts require a matching **`delhivery_lr`** row (by **`lrnum`** in the payload, or **`waybill`** matching **`delhivery_lr.waybill`**); otherwise the message is acknowledged and **`delhivery_pod_skipped`** is logged.

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

Example bodies (Delhivery may send base64 **and/or** a signed URL; form-style fields are kept in **`raw_payload`**):

```json
{
  "waybill": "1234567890",
  "EPOD": "<base64>",
  "orderID": "ORD-99",
  "lrnum": "220256789"
}
```

```json
{
  "lrnum": "220256789",
  "PODUrl": "https://…signed-s3-or-cdn-url…"
}
```

Each accepted push appends **`delhivery_lr_pod`** and moves **`delhivery_lr.latest_pod_id`** to that row’s **`id`** (history preserved for re-triggered PODs).

## Lambda configuration

- **`DATABASE_URL`** — PostgreSQL DSN (RDS / RDS Proxy recommended).
- **`DELHIVERY_WEBHOOK_SECRET`** — optional; verified in the **worker** using message attributes (see above).
- **`POD_S3_BUCKET`** — optional; when set, POD webhooks that include base64 **EPOD** are decoded and stored in this bucket, and Postgres gets a stable HTTPS **`pod_url`** (see below). Default at deploy time if unset: **`delivery-api-<stage>-delhivery-epod`** (IAM is scoped to that name unless you override **`POD_S3_BUCKET`**).
- **`POD_S3_PREFIX`** — optional object key prefix (default **`delhivery-pod`**). Objects look like **`{prefix}/{lrnum-or-waybill}/{uuid}.pdf`** (or **`.jpg`** / **`.png`** / **`.bin`** from content sniffing).

**SAP Business One Service Layer (optional)** — after a POD row is inserted, the worker can **PATCH** `Invoices(DocEntry)` when **`delhivery_lr.doc_entry`** is set and either:

**A) Environment variables (classic)** — all of these non-empty:

- **`SAP_SL_BASE_URL`** — e.g. `https://<host>:50000/b1s/v1` (no trailing slash).
- **`SAP_SL_COMPANYDB`**, **`SAP_SL_USERNAME`**, **`SAP_SL_PASSWORD`** — **Login** body fields.

**B) SSM Parameter Store (shared session across Lambdas)** — set **`SAP_SL_USE_SSM`** to **`1`**, **`true`**, or **`yes`**. Then the worker reads:

| Parameter | JSON keys |
|-----------|-----------|
| **`/sap/sl_config`** | `sap_b1_url`, `companydb` (aliases: `SAP_SL_BASE_URL`, `CompanyDB`) |
| **`/sap/credentials`** | `username`, `password` (aliases: `UserName`, `Password`) |
| **`/sap/b1_session`** | `b1session`, `routeid`, `expires_on` (UTC ISO recommended, e.g. `2026-06-03T14:00:00Z`), `is_locked` (`true`/`false`) |

On each POD sync it uses a cached session when **`expires_on`** is still in the future; otherwise it sets **`is_locked`**, calls **Login**, and updates **`/sap/b1_session`**. You do **not** need **`SAP_SL_*`** env vars when SSM mode is on (they may remain empty).

If SAP HTTPS returns **`SSL: CERTIFICATE_VERIFY_FAILED` / self-signed certificate** in CloudWatch (`sap_sl_ssm_renew_failed`), either install a **publicly trusted** cert on the Service Layer host, or for **dev only** set Lambda env **`SAP_SL_VERIFY_SSL`** to **`false`** (or **`0`**) so urllib skips TLS verification (see **`serverless.yml`** `SAP_SL_VERIFY_SSL`).

**VPC:** **`delhiveryWebhookWorker`** is in a private subnet. To reach **SSM** (`ssm.GetParameter` / `PutParameter`), add a **VPC interface endpoint** for **`com.amazonaws.<region>.ssm`** (and typically **SSM Messages**, **`com.amazonaws.<region>.ec2messages`** if you use Session Manager — not required for Parameter Store API alone) **or** route outbound HTTPS via a **NAT gateway**. Without one of these, SSM calls from the worker will time out.

Flow: **Login** (or reuse SSM session) → **GET** `Invoices(docEntry)?$select=DocEntry,U_LRtrackno` → compare **`U_LRtrackno`** to the resolved **`lrnum`** → **PATCH** same resource with **`U_Pod_Link`** (from **`pod_url`**), **`U_POD_DATE`** (from webhook-derived **`pod_date`**, ISO `YYYY-MM-DD` or `null`), **`U_LRtrackno`**, and when status is known **`U_FrappeGRNKey`**: **`1`** when Delhivery status is **Delivered**, **`2`** when it is **RTO** (match is case-insensitive on trim). Status is taken from the POD webhook **`payload.status`** (or **`delhivery_status`**) when present, otherwise from **`delhivery_lr.delhivery_status`** in RDS (e.g. after shipment-status webhooks). If neither yields **Delivered** or **RTO**, **`U_FrappeGRNKey`** is omitted from the PATCH. The outcome is stored on the **POD row** in **`delhivery_lr_pod.sap_pod_response`** (HTTP status, phase, `doc_entry`, optional error / response preview, `synced_at`). **`delhivery_lr.sap_response`** is left to **Frappe** for LR-level SAP payloads. Failures also log **`delhivery_sap_pod_*`** and do **not** fail the SQS message (POD is already in Postgres).

`serverless.yml` passes **`SAP_SL_*`** and **`SAP_SL_USE_SSM`** from the shell environment at deploy time (`${env:...}`). Set them in GitHub Actions / your deploy shell. IAM includes **`ssm:GetParameter`** / **`ssm:PutParameter`** on **`arn:aws:ssm:<region>:*:parameter/sap/sl_config`**, **`.../sap/credentials`**, **`.../sap/b1_session`**. Shared SAP helpers live in **`khanal_shared.sap_sl`** (see repo **`shared/khanal_shared/sap_sl/`**); they are copied into the Lambda layer by **`scripts/build_shared_layer.sh`**.

## AWS: S3 bucket + VPC for POD uploads

**`delhiveryWebhookWorker`** runs in a **VPC** to reach RDS. S3 is reached over the AWS network, not the public internet from the function’s subnets.

1. **Create an S3 bucket** (same region as the stack, e.g. `us-east-1`):
   - Name it either the **default** expected by deploy (**`delivery-api-<stage>-delhivery-epod`**) **or** any name you prefer; if you use a custom name, set **`POD_S3_BUCKET=<that-name>`** when you run **`serverless deploy`** so Lambda env and IAM match the bucket.
   - **Block Public Access** on (recommended). The worker stores a **virtual-hosted HTTPS URL** in **`pod_url`**; objects stay private unless you add CloudFront or presigned GET logic in your app.
   - Default encryption (SSE-S3 or SSE-KMS) is fine; the worker uses ordinary **`PutObject`** (no extra KMS IAM in the template yet — if you enforce KMS on the bucket, add **`kms:Encrypt`/`kms:GenerateDataKey`** on the key to the Lambda role).

2. **VPC connectivity to S3** (required for private subnets without a NAT gateway):
   - VPC → **Endpoints** → **Create endpoint** → service **`com.amazonaws.<region>.s3`** → type **Gateway**.
   - Attach it to the **route tables** used by the Lambda subnets (same subnets as in **`serverless.yml`**). Without this (and without NAT), **`PutObject`** will time out.

3. **IAM**: the Serverless stack grants **`s3:PutObject`** on **`arn:aws:s3:::<POD_S3_BUCKET>/*`**. No change needed in the console if you deploy after the bucket exists and names align.

4. **Redeploy** `delivery-api` after **`POD_S3_BUCKET`** / bucket creation / VPC endpoint changes.

5. **Optional GitHub Actions**: add repository secrets **`POD_S3_BUCKET`** and **`POD_S3_PREFIX`** and pass them in the deploy workflow `env` block so prod/staging use the right bucket.

**Note:** Delhivery **EPOD** is usually PDF bytes after base64 decode; it can be image data. Unknown binary is uploaded as **`application/octet-stream`** with a **`.bin`** suffix. If decode or upload fails, the worker logs **`delhivery_pod_s3_upload_failed`** and **re-raises** so SQS retries (then DLQ after max receives).

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
