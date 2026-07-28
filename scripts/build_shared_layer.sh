#!/usr/bin/env bash
# Build the shared Python Lambda layer contents (run from repo root).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT}/layers/build"
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}/python"
# psycopg + requests: pinned in layers/requirements.txt so every build (local,
# CI, deploy) installs identical versions — bump the pin, don't loosen the range.
# --platform/--only-binary force Lambda-compatible manylinux wheels even when
# this script runs on a non-Linux dev machine (psycopg[binary] has a C
# extension; a native macOS build silently produces a darwin .so that fails
# to import at Lambda runtime).
python3 -m pip install -r "${ROOT}/layers/requirements.txt" -t "${BUILD_DIR}/python" --quiet \
  --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp --only-binary=:all:
# HANA driver for Lambda (Linux x86_64). Pin kept in sync with layers/requirements.txt's
# comment. On macOS, cross-install may fail — run this script in CI/Linux.
if [[ "${INSTALL_HDBCLI:-1}" != "0" ]]; then
  python3 -m pip install "hdbcli==2.29.25" -t "${BUILD_DIR}/python" --quiet \
    --platform manylinux2014_x86_64 --python-version 3.12 --implementation cp \
    --only-binary=:all: 2>/dev/null \
    || echo "Note: skipped hdbcli (set INSTALL_HDBCLI=0 to silence; build layer on Linux for HANA)."
fi
cp -R "${ROOT}/shared/khanal_shared" "${BUILD_DIR}/python/"
echo "Built shared layer at ${BUILD_DIR} (python/khanal_shared + psycopg)."
