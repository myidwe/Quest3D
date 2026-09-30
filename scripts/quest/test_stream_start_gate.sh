#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$QUEST_CACHE/stream-start-tests"
g++ -std=c++17 -O1 -g -Wall -Wextra -Werror -pthread \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream/src" \
  "$QUEST_SCRIPTS/test_stream_start_gate.cpp" \
  -o "$QUEST_CACHE/stream-start-tests/start-gate"
"$QUEST_CACHE/stream-start-tests/start-gate"
