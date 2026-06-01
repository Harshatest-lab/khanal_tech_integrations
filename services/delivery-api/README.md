# delivery-api (AWS Lambda + API Gateway)

**Delhivery** shipment status + POD webhooks only. **Dogseeus_Invoice** is a **separate** stack: [../dogseeus-invoice](../dogseeus-invoice).

**Delhivery LRN** (Frappe writes tracking snapshots to Postgres; Lambdas handle Delhivery webhooks): [../../docs/DELHIVERY.md](../../docs/DELHIVERY.md).

| Logical name in `serverless.yml` | Python |
|----------------------------------|--------|
| `delhiveryWebhookIngest` | `src.delhivery_webhook_ingest.handler` (both HTTP routes; no VPC) |
| `delhiveryWebhookWorker` | `src.delhivery_sqs_worker.handler` (SQS → RDS; VPC + layer) |

## Why `package.json` / Node?

**Lambda runs Python 3.12.** Node is only for the **Serverless CLI** (`npx serverless deploy`), not inside Lambda.

## Deploy

```bash
# From repo root
bash scripts/build_shared_layer.sh
cd services/delivery-api
npm install
npx serverless deploy --stage dev
```

## Tests

From repo root: `pytest -q` (includes `services/delivery-api/tests`).
