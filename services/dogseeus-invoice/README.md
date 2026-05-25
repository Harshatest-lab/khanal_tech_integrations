# dogseeus-invoice (separate AWS stack)

**Dogseeus_Invoice** is its own **Serverless service**: this folder is deployed independently from `delivery-api` and creates its **own** CloudFormation stack, API Gateway, and Lambda(s).

| Item | Location |
|------|----------|
| Stack name (example) | `dogseeus-invoice-dev` |
| HTTP route | `POST /v1/dogseeus/invoices` |
| Python entry | `dogseeus_invoice/handler.py` → `invoice_handler` |

## Deploy

From repo root:

```bash
bash scripts/build_shared_layer.sh
cd services/dogseeus-invoice
npm install
npx serverless deploy --stage dev
```

Same **shared Lambda layer** pattern as `delivery-api` (`../../layers/build`).
