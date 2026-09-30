#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
g++ -std=c++23 -O2 -Wall -Wextra -Werror -static \
  -I"$TASK_ROOT/third_party/sunshine" \
  "$TASK_ROOT/native/audio/pcm_bridge_probe.cpp" \
  -lopus -lcrypto -lws2_32 -lcrypt32 -lbcrypt \
  -o "$TASK_ROOT/native/audio/dist/pcm_bridge_probe.exe"
