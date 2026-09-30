#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
g++ -std=c++23 -O2 -Wall -Wextra -static -pthread \
 -I"$TASK_ROOT/third_party/sunshine/cmake-build-quest3d/_deps/json-src/include" \
 "$TASK_ROOT/native/host/dxgi-snapshot-probe.cpp" \
 -ld3d11 -ldxgi -ldxguid -ldwmapi -lpsapi -lgdi32 -luser32 \
 -o "$TASK_ROOT/artifacts/host/dxgi-snapshot-probe.exe"
