#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_SOURCE="$TASK_ROOT/third_party/sunshine"
g++ -std=c++23 -O2 -Wall -Wextra -Werror -static -pthread \
  -iquote "$TASK_SOURCE/src" -iquote "$TASK_SOURCE/src/platform/windows" \
  -I"$TASK_SOURCE/cmake-build-quest3d/_deps/json-src/include" \
  "$TASK_SOURCE/src/quest3d_control.cpp" \
  "$TASK_SOURCE/src/platform/windows/quest3d_frame.cpp" \
  "$TASK_ROOT/native/host/control_bridge_probe.cpp" \
  -o "$TASK_ROOT/artifacts/host/control_bridge_probe.exe"
