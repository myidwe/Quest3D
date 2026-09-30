#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
cd "$QUEST_CACHE/source"
bash extensions/nightfall-xr/build_android.sh debug
