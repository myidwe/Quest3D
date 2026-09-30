#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
src="$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream"
out="$QUEST_ARTIFACTS/file-audio-transport"
mkdir -p "$out"
sources=("$src/src/network/file_audio_transport.h" "$src/src/network/file_audio_transport.cpp"
  "$src/src/network/curl_http_client.h" "$src/src/network/curl_http_client.cpp" "$src/src/audio/file_audio_protocol.h"
  "$QUEST_SCRIPTS/test_file_audio_transport.cpp" "$QUEST_SCRIPTS/test_file_audio_transport.py" "$QUEST_SCRIPTS/test_file_audio_transport.sh")
sha256sum "${sources[@]}" > "$out/SOURCE_SHA256SUMS"
flags=(-std=c++17 -O1 -g -pthread -Wall -Wextra -Werror -Wno-unused-parameter -fsanitize=address,undefined -fno-omit-frame-pointer
  -I"$QUEST_CACHE/sysroot/usr/include" -I"$QUEST_CACHE/sysroot/usr/include/x86_64-linux-gnu"
  -I"$src/include" -I"$src/src" -I"$src/src/network")
g++ "${flags[@]}" "$QUEST_SCRIPTS/test_file_audio_transport.cpp" \
  "$src/src/network/file_audio_transport.cpp" "$src/src/network/curl_http_client.cpp" \
  -Wl,--wrap=pthread_create -Wl,--wrap=pthread_join -Wl,-l:libcurl.so.4 -Wl,-l:libssl.so.3 -Wl,-l:libcrypto.so.3 -o "$out/file-audio-client"
g++ "${flags[@]}" -Wno-unused-parameter "$QUEST_SCRIPTS/test_tls_client.cpp" "$src/src/network/curl_http_client.cpp" \
  -Wl,-l:libcurl.so.4 -Wl,-l:libssl.so.3 -Wl,-l:libcrypto.so.3 -o "$out/tls-client"
ASAN_OPTIONS=detect_leaks=1 python3 "$QUEST_SCRIPTS/test_file_audio_transport.py" 2>&1 | tee "$out/test.log"
g++ "${flags[@]}" -fno-exceptions "$QUEST_SCRIPTS/test_file_audio_transport.cpp" \
  "$src/src/network/file_audio_transport.cpp" "$src/src/network/curl_http_client.cpp" \
  -Wl,--wrap=pthread_create -Wl,--wrap=pthread_join -Wl,-l:libcurl.so.4 -Wl,-l:libssl.so.3 -Wl,-l:libcrypto.so.3 -o "$out/file-audio-client-no-exceptions"
ASAN_OPTIONS=detect_leaks=1 FILE_AUDIO_NO_EXCEPTIONS=1 python3 "$QUEST_SCRIPTS/test_file_audio_transport.py" 2>&1 | tee "$out/test-no-exceptions.log"
sha256sum "${sources[@]}" > "$out/SOURCE_SHA256SUMS.after"
cmp "$out/SOURCE_SHA256SUMS" "$out/SOURCE_SHA256SUMS.after"
