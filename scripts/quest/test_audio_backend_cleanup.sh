#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$root/artifacts/quest/audio-cleanup"
mkdir -p "$out"
src="$root/third_party/nightfall/addons/nightfall-stream"
# Null backend only: never open ALSA/PulseAudio/AAudio/OS audio devices.
c++ -std=c++17 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer \
  -DMA_ENABLE_ONLY_SPECIFIC_BACKENDS -DMA_ENABLE_NULL \
  -I"$src/include" -I"$src/src" \
  "$root/scripts/quest/test_audio_backend_cleanup.cpp" "$src/src/audio/miniaudio_backend.cpp" \
  -pthread -ldl -lm -o "$out/backend-cleanup-test"
ASAN_OPTIONS=detect_leaks=1 "$out/backend-cleanup-test" | tee "$out/backend-test.log"
