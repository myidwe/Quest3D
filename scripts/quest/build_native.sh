#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$QUEST_CACHE/source"
bash tools/build_support/bootstrap_native_xr.sh
