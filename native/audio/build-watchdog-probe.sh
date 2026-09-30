#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
g++ -std=c++23 -O2 -Wall -Wextra -Werror -static \
  -I"$TASK_ROOT/third_party/sunshine/src/platform/windows" \
  "$TASK_ROOT/native/audio/audio_watchdog_probe.cpp" \
  -o "$TASK_ROOT/native/audio/dist/audio_watchdog_probe.exe"
