# khanal-shared

Install editable from repo root:

```bash
pip install -e "./shared[dev]"
```

This package is bundled into Lambdas via `package.patterns` in each `serverless.yml`, or via a **Lambda Layer** (recommended at scale — see `layers/shared/README.md`).

## Layout

- **`khanal_shared.sap_sl`** — SAP B1 Service Layer **urllib** helpers (`http`), **SSM session cache** (`ssm_session`, env `SAP_SL_USE_SSM`), and **env detection + env login** (`config`: `sap_sl_enabled`, `build_opener_and_base_from_env`). Used by `delivery-api` and any other service that attaches the shared layer.
- **`khanal_shared.hana`** — SAP HANA **read-only** DB (`hdbcli`), connection pool (`pool`), credentials from **SSM** (`/sap/hana_config` with `host`, `port`, `database`, `schema`; `/sap/hana_credentials`; env `HANA_USE_SSM`) or **`HANA_*`** env vars for local dev.
