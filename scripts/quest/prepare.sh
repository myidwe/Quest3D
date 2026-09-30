#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
for command in git curl tar python3 dpkg-deb gcc g++ make rg; do
  command -v "$command" >/dev/null || { echo "Missing WSL prerequisite: $command" >&2; exit 1; }
done
python3 "$QUEST_SCRIPTS/bootstrap.py"
source "$QUEST_SCRIPTS/env.sh"
bash "$QUEST_SCRIPTS/setup_sdk.sh"
bash "$VCPKG_ROOT/bootstrap-vcpkg.sh" -disableMetrics
cd "$QUEST_CACHE/source"
bash tools/build_support/bootstrap_native_xr.sh --sources-only
printf '%s\n' "Prepared isolated build cache: $QUEST_CACHE"
