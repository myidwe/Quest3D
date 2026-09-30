#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TASK_OUT="$TASK_ROOT/artifacts/host"
g++ -std=c++23 -O2 -Wall -Wextra -Werror -static -pthread -shared \
 "$TASK_ROOT/native/host/dxgi_snapshot_capture.cpp" -ld3d11 -ldxgi -ldxguid -luser32 \
 -Wl,--out-implib,"$TASK_OUT/libdxgi_snapshot_capture.a" -o "$TASK_OUT/dxgi_snapshot_capture.dll"
g++ -std=c++23 -O2 -Wall -Wextra -Werror -static -pthread \
 "$TASK_ROOT/native/host/dxgi_snapshot_capture_test.cpp" -ld3d11 -ldxgi -ldxguid -luser32 \
 -o "$TASK_OUT/dxgi_snapshot_capture_test.exe"
if [[ -f "$TASK_ROOT/native/host/dxgi-snapshot-capture-probe.cpp" ]]; then
 g++ -std=c++23 -O2 -Wall -Wextra -Werror -static -pthread \
  -I"$TASK_ROOT/third_party/sunshine/cmake-build-quest3d/_deps/json-src/include" \
  "$TASK_ROOT/native/host/dxgi-snapshot-capture-probe.cpp" "$TASK_OUT/libdxgi_snapshot_capture.a" \
  -ld3d11 -ldxgi -ldxguid -ldwmapi -lpsapi -lgdi32 -luser32 \
  -o "$TASK_OUT/dxgi-snapshot-capture-probe.exe"
fi
