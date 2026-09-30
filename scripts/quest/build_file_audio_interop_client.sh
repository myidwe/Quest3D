#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
src="$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream"
out="$QUEST_PROJECT/artifacts/host/cmake-build-file-audio-interop-20260910-a"
mkdir -p "$out"
sources=("$src/src/network/file_audio_transport.h" "$src/src/network/file_audio_transport.cpp"
  "$src/src/network/curl_http_client.h" "$src/src/network/curl_http_client.cpp" "$src/src/audio/file_audio_protocol.h"
  "$QUEST_SCRIPTS/file_audio_interop_client.cpp" "$QUEST_SCRIPTS/build_file_audio_interop_client.sh")
sha256sum "${sources[@]}" > "$out/CLIENT_SOURCE_SHA256SUMS"
g++ -std=c++17 -O1 -g -pthread -Wall -Wextra -Werror -Wno-unused-parameter -fno-exceptions \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$QUEST_CACHE/sysroot/usr/include" -I"$QUEST_CACHE/sysroot/usr/include/x86_64-linux-gnu" \
  -I"$src/include" -I"$src/src" -I"$src/src/network" \
  "$QUEST_SCRIPTS/file_audio_interop_client.cpp" "$src/src/network/file_audio_transport.cpp" "$src/src/network/curl_http_client.cpp" \
  -Wl,-l:libcurl.so.4 -Wl,-l:libssl.so.3 -Wl,-l:libcrypto.so.3 -o "$out/file-audio-client"
sha256sum "${sources[@]}" > "$out/CLIENT_SOURCE_SHA256SUMS.after"
cmp "$out/CLIENT_SOURCE_SHA256SUMS" "$out/CLIENT_SOURCE_SHA256SUMS.after"
sha256sum "$out/file-audio-client" > "$out/CLIENT_BINARY_SHA256SUMS"
