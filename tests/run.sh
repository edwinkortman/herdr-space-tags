#!/usr/bin/env bash
# Run the unit tests. No Herdr server is contacted; the Herdr CLI is stubbed
# and socket requests run in dry-run mode.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 -m unittest discover -s tests -v
