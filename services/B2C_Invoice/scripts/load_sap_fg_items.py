#!/usr/bin/env python3
"""Local-only: sync SAP FG items (HANA OITM) into Postgres ``sap_fg_items`` table.

Not used by Lambda. Run from repo after creating the table:

  psql "$DATABASE_URL" -f sql/sap_fg_items.sql

  cd services/B2C_Invoice
  source ../../.venv/bin/activate
  pip install boto3 'psycopg[binary]' hdbcli
  export DATABASE_URL='postgresql://...'
  export HANA_USE_SSM=1
  export AWS_DEFAULT_REGION=us-east-1

  ../../.venv/bin/python scripts/load_sap_fg_items.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REQUIRED = ("psycopg", "botocore", "hdbcli")
_missing = []
for _mod in _REQUIRED:
    try:
        __import__(_mod)
    except ImportError:
        _missing.append(_mod)
if _missing:
    print(
        f"Missing Python packages: {', '.join(_missing)}\n"
        f"Using interpreter: {sys.executable}\n"
        "Fix (from repo root):\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install boto3 'psycopg[binary]' hdbcli\n"
        "  cd services/B2C_Invoice\n"
        "  ../../.venv/bin/python scripts/load_sap_fg_items.py",
        file=sys.stderr,
    )
    raise SystemExit(1)

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_REPO_SHARED = Path(__file__).resolve().parent.parent.parent.parent / "shared"
if _REPO_SHARED.is_dir() and str(_REPO_SHARED) not in sys.path:
    sys.path.insert(0, str(_REPO_SHARED))

from walmart.sap_fg_item_store import load_sap_fg_items_from_hana


def main() -> int:
    result = load_sap_fg_items_from_hana()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
