#!/usr/bin/env bash
# Build the shared Python Lambda layer contents (run from repo root).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT}/layers/build"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/python"
# psycopg in layer so Lambdas can use RDS / RDS Proxy without per-function packaging plugins.
python3 -m pip install "psycopg[binary,pool]>=3.2,<4" -t "${BUILD_DIR}/python" --quiet
# requests for the amazon-us-b2c SP-API client.
python3 -m pip install "requests>=2.31,<3" -t "${BUILD_DIR}/python" --quiet
# HANA driver for Lambda (Linux x86_64). On macOS, cross-install may fail — run this script in CI/Linux.
if [[ "${INSTALL_HDBCLI:-1}" != "0" ]]; then
  python3 -m pip install "hdbcli>=2.23" -t "${BUILD_DIR}/python" --quiet \
    --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp \
    --only-binary=:all: 2>/dev/null \
    || echo "Note: skipped hdbcli (set INSTALL_HDBCLI=0 to silence; build layer on Linux for HANA)."
fi
cp -R "${ROOT}/shared/khanal_shared" "${BUILD_DIR}/python/"
echo "Built shared layer at ${BUILD_DIR} (python/khanal_shared + psycopg)."
