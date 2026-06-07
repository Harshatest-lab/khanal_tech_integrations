# Khanal Tech — Serverless backend (monorepo)

**Python 3.12** · **AWS Lambda** · **API Gateway** · **Serverless Framework** · **GitHub Actions**

## Services (two stacks)

| Directory | Purpose |
|-----------|---------|
| [services/delivery-api](services/delivery-api) | Shipments, carrier webhooks, **Delhivery ingest + webhooks** — [README](services/delivery-api/README.md), [DELHIVERY](docs/DELHIVERY.md) |
| [services/dogseeus-invoice](services/dogseeus-invoice) | **Dogseeus_Invoice** (stub) — own stack — [README](services/dogseeus-invoice/README.md) |

### Python vs Node in this repo

- **You develop in Python:** `services/*/src/*.py`, `shared/khanal_shared/`, **pytest**.
- **Node/npm** per service folder only runs the **Serverless CLI** for deploy/package. **Lambda runtime is Python** (`python3.12` in each `serverless.yml`).

Shared library: [shared/khanal_shared](shared/khanal_shared) (packaged as a **Lambda layer** — run `bash scripts/build_shared_layer.sh` before deploy).

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — layout, deployment, diagrams  
- [docs/DELHIVERY.md](docs/DELHIVERY.md) — Delhivery LRN: Frappe writes tracking snapshots to Postgres; Lambdas handle Delhivery webhooks  
- [docs/DATABASE.md](docs/DATABASE.md) — PostgreSQL / RDS Proxy reference  
- [docs/INFRA_AWS_FROM_SCRATCH.md](docs/INFRA_AWS_FROM_SCRATCH.md) — **VPC, RDS, security groups, IAM, S3/SSM endpoints** in order (greenfield / prod)  
- [docs/LOCAL_DEV.md](docs/LOCAL_DEV.md) — local workflow  

## Quick commands

```bash
# Tests
pip install -e "./shared[dev]"
pytest -q

# Build shared layer, then deploy one stack
bash scripts/build_shared_layer.sh
cd services/delivery-api && npm install && npx serverless deploy --stage dev
# or
cd services/dogseeus-invoice && npm install && npx serverless deploy --stage dev
```

## License

Proprietary — adjust as needed.
