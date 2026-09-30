#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$QUEST_CACHE/tls-tests"
g++ -std=c++17 -O1 -pthread \
  -I"$QUEST_CACHE/sysroot/usr/include" -I"$QUEST_CACHE/sysroot/usr/include/x86_64-linux-gnu" \
  -I"$QUEST_CACHE/source/addons/nightfall-stream/include" \
  -I"$QUEST_CACHE/source/addons/nightfall-stream/src/network" \
  "$QUEST_SCRIPTS/test_tls_client.cpp" \
  "$QUEST_CACHE/source/addons/nightfall-stream/src/network/curl_http_client.cpp" \
  -Wl,-l:libcurl.so.4 -Wl,-l:libssl.so.3 -Wl,-l:libcrypto.so.3 \
  -o "$QUEST_CACHE/tls-tests/tls-client"
python3 "$QUEST_SCRIPTS/test_tls.py"
g++ -std=c++17 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$QUEST_CACHE/source/addons/nightfall-stream/src" \
  "$QUEST_CACHE/source/test/pairing_protocol_test.cpp" \
  -o "$QUEST_CACHE/tls-tests/pairing-protocol"
"$QUEST_CACHE/tls-tests/pairing-protocol"
