# Local development workflow

## Prerequisites

- Python 3.12
- Node.js 20+
- Docker (optional, for Postgres)

## Python env

```bash
cd /path/to/Khanal_tech_serverless
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e "./shared[dev]"
```

## Tests

```bash
pytest -q
```

## Build the shared Lambda layer

Required before `serverless deploy` / `serverless package`:

```bash
bash scripts/build_shared_layer.sh
```

## Serverless (per service)

Each folder under `services/` is its **own** deployable stack.

```bash
cd services/delivery-api        # or services/dogseeus-invoice
npm install
npx serverless package --stage dev
# npx serverless deploy --stage dev
```

## Environment

Copy [.env.example](.env.example) to `.env` for local tools (never commit secrets).
