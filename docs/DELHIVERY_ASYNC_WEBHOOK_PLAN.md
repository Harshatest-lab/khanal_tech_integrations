# Plan: Async Delhivery webhooks (API → SQS → worker Lambda)

**Status:** Implemented in `delivery-api` — **REST API Gateway → SQS** (no ingest Lambda on the hot path) + worker (`delhiveryWebhookWorker`). Matches **Option A / C** from below (native SQS integration + VTL response).

Goal: **Acknowledge Delhivery in &lt; 500 ms** (ideally &lt; 50 ms) without paying for always-warm VPC Lambda on the **synchronous** path. **DB work** runs in the background with cold starts acceptable.

Current state: **HTTP API** → single **VPC Lambda** per route → **RDS** (cold start + VPC ENI + DB pool dominates latency).

---

## Target architecture (your Phase 1 + Phase 2)

```text
Phase 1 — Ingest (fast)
  Delhivery POST
    → API Gateway
        → (A) validate optional secret *if* you keep validation here
        → enqueue full request (body + needed headers)
        → HTTP 200 + small JSON body

Phase 2 — Worker (slow OK)
  SQS
    → Lambda (VPC, shared layer, psycopg)
        → same business logic as today: normalize → delhivery_lr / events / pod tables
    → delete message on success; DLQ on poison / max receives
```

---

## Design choices (pick one for Phase 1)

### Option A — **True “no Lambda” ingest** (API Gateway → SQS only)

- **Pros:** Lowest latency and cost on ack path; no cold start on receive.
- **Cons:**
  - **HTTP API (API Gateway v2)** direct SQS integration is possible but **mapping templates / parameter mapping** differ from REST v1; you must verify in AWS docs for your exact “echo `lrnum` in JSON response” requirement.
  - **`X-Delhivery-Webhook-Secret`** cannot be checked with `hmac.compare_digest` in VTL the way you do in Python today. Practical approaches:
    - **Drop sync secret check** and validate only in the worker (caller already got `200` — only use if Delhivery retries are acceptable for abuse, or you add another control).
    - Or put a **shared secret in a query string** Delhivery can configure (if their product allows URL params) and use a **mapping** + simple match (still weak vs timing attacks; prefer worker-only validation + rate limits).
  - Returning `{"ok": true, "lrnum": "<from body>"}` may require **Velocity / JSONata** parsing of `$request.body` in the integration response — doable but fiddly; alternatively return fixed `{"ok": true}` and document that Delhivery does not need `lrnum` echoed.

**When to choose A:** You accept simpler 200 body or invest in API mapping; secret validation mainly in worker or via URL token.

### Option B — **Minimal “ingest” Lambda** (recommended pragmatic default)

- **One tiny function** (no VPC, **no** shared DB layer, **no** `psycopg`):
  - Parse JSON (stdlib).
  - Optional: `hmac.compare_digest` for `X-Delhivery-Webhook-Secret` (copy small helper from `src/common.py` without pulling `khanal_shared` if you want smallest zip — or keep a 20-line inline).
  - `boto3.client("sqs").send_message(QueueUrl=..., MessageBody=json.dumps({...}))`.
  - Return `200` + `{"ok": true, "lrnum": ...}` or `400` if body invalid / missing `lrnum`.
- **Pros:** Keeps current auth semantics; predictable; still **much** faster and cheaper cold start than VPC + layer + DB init.
- **Cons:** Still one Lambda on hot path — typically **tens to low hundreds of ms** cold, often **under 500 ms** with small package; use **arm64** + **minimal deps** + avoid imports at module top except boto3.

**When to choose B:** You want **&lt; 500 ms** reliably with minimal architecture risk; sub-50 ms is not guaranteed without provisioned concurrency or Option A.

### Option C — **REST API (v1)** + SQS integration + VTL response

- Mature pattern for “map body to SQS + return static/VTL JSON”.
- **Cons:** Second API type or migration from HTTP API; more CloudFormation / Serverless `resources` wiring.

---

## Phase 2 — Worker Lambda (unchanged semantics)

1. **Trigger:** SQS event source mapping on the queue (batch size 1–10; start with **1** for simpler error handling).
2. **Runtime:** Same as today: Python 3.12, **VPC** + **RDS** security groups, **`DATABASE_URL`**, shared **Lambda layer** with `khanal_shared` + `psycopg`.
3. **Handler contract:** Message body is a small envelope, e.g.:

   ```json
   {
     "kind": "shipment_status",
     "receivedAt": "2026-06-01T12:00:00Z",
     "headers": { "x-delhivery-webhook-secret": "..." },
     "body": { ... original Delhivery JSON ... }
   }
   ```

   Worker branches on `kind` and calls existing logic:
   - `shipment_status` → `normalize_shipment_webhook` → `apply_shipment_status_webhook`
   - `pod` → `normalize_pod_webhook` → `apply_pod_webhook`

4. **Secret validation:** If ingest did not validate, worker runs the same `delhivery_secret_error_response` rules: if secret is configured and wrong, **do not** write DB; send message to **DLQ** or delete with log (define policy — avoid infinite retries on bad secret).

5. **Idempotency:** SQS is **at-least-once**. Shipment path is already **upsert + insert event** — duplicates create extra audit rows; acceptable or add dedupe key in message / DB if needed. POD path is append-only — duplicates may duplicate rows; consider **message deduplication id** (FIFO queue) or idempotency key in payload if Delhivery retries same event.

6. **DLQ:** Configure **redrive policy** (e.g. max receives 3–5) to a **DLQ**; alarm on DLQ depth.

7. **Cold start / init:** Move **all** DB and heavy imports **inside** the worker handler (or lazy-init on first invocation) so **import time** does not run for every cold start before SQS batch is parsed — same guidance you quoted for reducing init duration.

---

## Infrastructure checklist (Serverless / IaC)

| Item | Notes |
|------|--------|
| **SQS queue** | e.g. `delhivery-webhook-{stage}`; **visibility timeout** ≥ worker Lambda timeout (e.g. 60s if Lambda timeout 30s). |
| **DLQ** | For worker failures after retries. |
| **IAM** | Ingest: `sqs:SendMessage` only on that queue. Worker: `sqs:ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`; existing VPC/RDS execution role. |
| **API route** | Replace current `httpApi` event on shipment-status (and optionally POD) with either Option A integration or Option B function URL / API integration. |
| **POD route** | Same pattern: same queue + `kind` discriminator, or **separate queue** + dedicated worker (clearer isolation). |

---

## Code refactor (repo)

1. **Extract pure functions** (no Lambda event shape) for “apply shipment” and “apply POD” from existing handlers — input: `dict` body + optional headers for secret.
2. **Worker handler** `src/delhivery_sqs_worker.handler`: loop `event["Records"]`, `json.loads(record["body"])`, dispatch by `kind`, call extracted functions inside `with pool.connection()`.
3. **Keep current handlers** behind a feature flag or remove after cutover; update **integration tests** to invoke worker with synthetic SQS events.
4. **Docs:** Update [DELHIVERY.md](DELHIVERY.md) with new flow and latency expectations.

---

## “Reduce package size” / “move logic inside handler”

| Concern | Ingest Lambda (Option B) | Worker Lambda |
|---------|---------------------------|----------------|
| Dependencies | **boto3** (+ stdlib only if possible) | Keep layer + psycopg; trim unused wheels if any |
| VPC | **No** | **Yes** (RDS) |
| Module-level DB | **None** | Lazy `get_pool()` inside handler path; avoid `ensure_warmed_for_lambda()` at import if it pulls DB |

---

## Rollout order

1. Add queue + worker + DLQ; deploy **without** switching Delhivery URL (worker can be tested by manually sending SQS messages).
2. Switch API route to ingest (A or B); **monitor** p99 ack latency and SQS `ApproximateAgeOfOldestMessage`.
3. Point Delhivery to new URL if URL changes; otherwise internal only.
4. Remove old synchronous Lambda wiring after stable window.

---

## Cost / SLA summary

| Path | Delhivery sees | DB write |
|------|----------------|----------|
| Today | Waits for VPC + DB | Same request |
| Async | Fast HTTP 200 | Seconds later acceptable |
| “Free &lt; 50 ms always” | Only guaranteed with **no** Lambda on ingest (Option A) or **provisioned concurrency** on a tiny ingest function |

---

## References

- **Implemented:** `services/delivery-api/serverless.yml` (REST API → SQS + IAM role for API Gateway) + `src/delhivery_sqs_worker.py` (SQS → RDS; raw body + message attributes).
- Store layer: `services/delivery-api/src/delhivery_store.py`
- [AWS: API Gateway HTTP API AWS integrations](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-develop-integrations-aws-services.html)
- [AWS: SQS + Lambda event source mapping](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html)
