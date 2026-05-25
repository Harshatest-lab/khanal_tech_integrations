#!/usr/bin/env bash
# Build the shared Python Lambda layer contents (run from repo root).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT}/layers/build"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/python"
# psycopg in layer so Lambdas can use RDS / RDS Proxy without per-function packaging plugins.
python3 -m pip install "psycopg[binary,pool]>=3.2,<4" -t "${BUILD_DIR}/python" --quiet
cp -R "${ROOT}/shared/khanal_shared" "${BUILD_DIR}/python/"
echo "Built shared layer at ${BUILD_DIR} (python/khanal_shared + psycopg)."
