#!/usr/bin/env bash
# Run SAP FG items load using repo-root .venv (avoids Homebrew python vs venv mismatch).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SERVICE="${ROOT}/services/B2C_Invoice"

pick_venv_python() {
  # This repo's .venv may have python -> 3.12 while `pip` installed into 3.13 — prefer 3.13 if present.
  for candidate in "${ROOT}/.venv/bin/python3.13" "${ROOT}/.venv/bin/python3.12" "${ROOT}/.venv/bin/python"; do
    if [[ -x "${candidate}" ]] && "${candidate}" -c "import botocore, psycopg, hdbcli" 2>/dev/null; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

VENV_PY="$(pick_venv_python || true)"
if [[ -z "${VENV_PY}" ]]; then
  echo "No usable venv Python found under ${ROOT}/.venv"
  echo "Fix:"
  echo "  cd ${ROOT} && rm -rf .venv && python3 -m venv .venv"
  echo "  ${ROOT}/.venv/bin/pip install boto3 'psycopg[binary]' hdbcli"
  exit 1
fi

cd "${SERVICE}"
export PYTHONPATH="${SERVICE}/src:${ROOT}/shared"
echo "Using ${VENV_PY}"
exec "${VENV_PY}" scripts/load_sap_fg_items.py
