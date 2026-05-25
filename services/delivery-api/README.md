# delivery-api (AWS Lambda + API Gateway)

Shipments and carrier webhooks. **Dogseeus_Invoice** is a **separate** stack: [../dogseeus-invoice](../dogseeus-invoice).

**Delhivery LRN** (Frappe snapshot + Delhivery shipment webhooks): [../../docs/DELHIVERY.md](../../docs/DELHIVERY.md).

| Logical name in `serverless.yml` | Python |
|----------------------------------|--------|
| `createShipment` | `src/handler.py` → `create_shipment_handler` |
| `carrierWebhook` | `src/handler.py` → `carrier_webhook_handler` |
| `frappeDelhiveryTrackingSnapshot` | `src/handler.py` → `frappe_delhivery_tracking_snapshot_handler` |
| `delhiveryShipmentStatusWebhook` | `src/handler.py` → `delhivery_shipment_status_webhook_handler` |

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
