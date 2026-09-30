#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
src="$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream"
out="$(mktemp -d "$QUEST_ARTIFACTS/file-audio-epoch-session-XXXXXXXX")"
printf 'OUTPUT=%s\n' "$out"
flags=(-std=c++17 -O1 -g -pthread -Wall -Wextra -Wno-unused-parameter
  -fno-exceptions -fsanitize=address,undefined -fno-omit-frame-pointer
  -DMA_ENABLE_ONLY_SPECIFIC_BACKENDS -DMA_ENABLE_NULL
  -I"$QUEST_CACHE/sysroot/usr/include" -I"$QUEST_CACHE/sysroot/usr/include/x86_64-linux-gnu"
  -I"$src/include" -I"$src/src" -I"$src/src/network")
inputs=("$QUEST_SCRIPTS/test_file_audio_epoch_session.cpp" "$src/src/audio/file_audio_epoch_session.cpp"
  "$src/src/audio/file_audio_sink.cpp" "$src/src/audio/miniaudio_backend.cpp"
  "$src/src/network/file_audio_transport.cpp" "$src/src/network/curl_http_client.cpp")
sha256sum "${inputs[@]}" "$src/src/audio/file_audio_protocol.h" "$src/src/audio/file_audio_sink.h" \
  "$src/src/audio/file_audio_epoch_session.h" "$src/src/network/file_audio_transport.h" \
  "$src/src/audio/audio_device_callbacks.h" "$src/include/miniaudio.h" > "$out/source-before.sha256"
g++ "${flags[@]}" "${inputs[@]}" -Wl,-l:libcurl.so.4 -Wl,-l:libssl.so.3 -Wl,-l:libcrypto.so.3 \
  -Wl,--wrap=pthread_join -ldl -lm -o "$out/epoch-test" > "$out/build.log" 2>&1
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 python3 "$QUEST_SCRIPTS/test_file_audio_epoch_session.py" "$out" \
  2>&1 | tee "$out/test.log"
sha256sum --check "$out/source-before.sha256" > "$out/source-after-check.log"
printf 'PASS_OUTPUT=%s\n' "$out"
