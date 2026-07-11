#!/usr/bin/env bash
# Serve dashboard locally — required because browsers block fetch() from file:// URLs.
cd "$(dirname "$0")"
echo "Dashboard: http://localhost:8080"
echo "Press Ctrl+C to stop."
exec python3 -m http.server 8080
