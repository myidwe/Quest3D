#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$root/artifacts/quest/audio-startup"
mkdir -p "$out"
src="$root/third_party/nightfall/addons/nightfall-stream"
for suite in startup cleanup; do
  c++ -std=c++17 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    -DMA_ENABLE_ONLY_SPECIFIC_BACKENDS -DMA_ENABLE_NULL \
    -I"$src/include" -I"$src/src" \
    "$root/scripts/quest/test_audio_backend_${suite}.cpp" "$src/src/audio/miniaudio_backend.cpp" \
    -pthread -ldl -lm -o "$out/backend-${suite}-test"
  ASAN_OPTIONS=detect_leaks=1 "$out/backend-${suite}-test" 2>&1 | tee "$out/backend-${suite}.log"
done
