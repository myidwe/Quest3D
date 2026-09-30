#!/usr/bin/env bash
# Invoked inside the isolated MSYS2 UCRT64 environment by build-host.ps1.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_SOURCE="$TASK_ROOT/third_party/sunshine"
TASK_BUILD="$TASK_SOURCE/cmake-build-quest3d"
TASK_ARTIFACTS="$TASK_ROOT/artifacts/host"
TASK_JOBS="${1:-8}"
TASK_MODE="${2:-all}"
export PATH="$TASK_ROOT/native/host/tools/node-v24.20.0-win-x64:$PATH"
export npm_config_cache="$TASK_ARTIFACTS/npm-cache"
mkdir -p "$TASK_ARTIFACTS" "$TASK_BUILD"
# Keep all configure-time Git subprocesses independent of unreadable user ignores.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.excludesFile
export GIT_CONFIG_VALUE_0="$TASK_ARTIFACTS/empty-git-ignore"
g++ -std=c++23 -O2 -Wall -Wextra -Werror -static \
  -I"$TASK_SOURCE/src/platform/windows" \
  "$TASK_SOURCE/src/platform/windows/quest3d_instance.cpp" \
  "$TASK_ROOT/native/host/instance_guard_probe.cpp" \
  -o "$TASK_ARTIFACTS/instance_guard_probe.exe"
pacman -Q > "$TASK_ARTIFACTS/msys2-packages.lock.txt"
git -C "$TASK_SOURCE" submodule status --recursive > "$TASK_ARTIFACTS/submodules.lock.txt"

# The bridge tests touch isolated per-process mappings only and never inject input.
g++ -std=c++23 -O2 -Wall -Wextra -Werror -static -pthread \
  -I"$TASK_SOURCE/src/platform/windows" \
  "$TASK_SOURCE/src/platform/windows/quest3d_frame.cpp" \
  "$TASK_SOURCE/src/platform/windows/quest3d_input.cpp" \
  "$TASK_ROOT/native/host/frame_bridge_test.cpp" \
  -o "$TASK_ARTIFACTS/frame_bridge_test.exe"
"$TASK_ARTIFACTS/frame_bridge_test.exe"
g++ -std=c++23 -O2 -Wall -Wextra -Werror -static -pthread \
  -I"$TASK_SOURCE/src/platform/windows" \
  "$TASK_SOURCE/src/platform/windows/quest3d_frame.cpp" \
  "$TASK_ROOT/native/host/frame_bridge_v3_test.cpp" \
  -o "$TASK_ARTIFACTS/frame_bridge_v3_test.exe"
"$TASK_ARTIFACTS/frame_bridge_v3_test.exe"
g++ -std=c++23 -O2 -Wall -Wextra -Werror -static \
  -I"$TASK_SOURCE/src/platform/windows" \
  "$TASK_SOURCE/src/platform/windows/quest3d_frame.cpp" \
  "$TASK_ROOT/native/host/frame_bridge_probe.cpp" \
  -lbcrypt \
  -o "$TASK_ARTIFACTS/frame_bridge_probe.exe"
if [[ "$TASK_MODE" == bridge ]]; then
  exit 0
fi

cd "$TASK_SOURCE"
export BRANCH=quest3d
export BUILD_VERSION=2026.906.222525
export COMMIT=cb72dffa3233c5815cd5ba88f09f049dd679ba75-quest3d
cmake -S "$TASK_SOURCE" -B "$TASK_BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_DOCS=OFF -DBUILD_TESTS=ON \
  -DSUNSHINE_ENABLE_TRAY=OFF -DSUNSHINE_USE_STATIC_QT=OFF \
  -DSUNSHINE_ENABLE_WIX=OFF \
  -DLIBVIRTUALHID_BUILD_WINDOWS_DRIVER=OFF \
  -DLIBVIRTUALHID_BUILD_WINDOWS_BROKER=OFF \
  -DLIBVIRTUALHID_BUILD_TOOLS=OFF \
  -DSUNSHINE_PUBLISHER_NAME=Quest3D \
  -DNPM="$TASK_ROOT/native/host/tools/node-v24.20.0-win-x64/npm.cmd"
cmake --build "$TASK_BUILD" --parallel "$TASK_JOBS" --target sunshine
if [[ "$TASK_MODE" == all ]]; then
  cmake --build "$TASK_BUILD" --parallel "$TASK_JOBS" --target web-ui test_sunshine
fi
