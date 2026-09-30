#!/usr/bin/env bash
# Build only the configured, pinned Sunshine candidate. Never deploy or launch it.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_BUILD="$TASK_ROOT/third_party/sunshine/cmake-build-quest3d"
TASK_ARTIFACTS="$TASK_ROOT/artifacts/host"
TASK_JOBS="${1:-6}"
if [[ ! "$TASK_JOBS" =~ ^[1-9][0-9]*$ ]]; then
  echo 'Expected a positive parallel job count' >&2
  exit 2
fi
if [[ ! -f "$TASK_BUILD/CMakeCache.txt" ]]; then
  echo 'Configure the pinned source first with native/host/build-host.sh (see docs/build/HOST.md).' >&2
  exit 2
fi
mkdir -p "$TASK_ARTIFACTS/npm-cache"
touch "$TASK_ARTIFACTS/empty-git-ignore"
export PATH="$TASK_ROOT/native/host/tools/node-v24.20.0-win-x64:$PATH"
export npm_config_cache="$TASK_ARTIFACTS/npm-cache"
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.excludesFile
export GIT_CONFIG_VALUE_0="$TASK_ARTIFACTS/empty-git-ignore"
cmake --build "$TASK_BUILD" --parallel "$TASK_JOBS" --target sunshine test_sunshine
