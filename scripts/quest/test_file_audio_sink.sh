#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$root/artifacts/quest/file-audio-sink"
out="$(mktemp -d "$root/artifacts/quest/file-audio-sink/run-XXXXXXXX")"
src="$root/third_party/nightfall/addons/nightfall-stream"
flags=(-std=c++17 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer
  -DMA_ENABLE_ONLY_SPECIFIC_BACKENDS -DMA_ENABLE_NULL -I"$src/include" -I"$src/src")
printf 'OUTPUT=%s\n' "$out"
python3 "$root/scripts/quest/file_audio_sink_source_manifest.py" "$out/source-before.json"
c++ "${flags[@]}" -c "$src/src/audio/miniaudio_backend.cpp" -o "$out/miniaudio_backend.o"
c++ "${flags[@]}" -c "$src/src/audio/file_audio_sink.cpp" -o "$out/file_audio_sink.o"
c++ "${flags[@]}" "$root/scripts/quest/test_file_audio_sink.cpp" "$out/miniaudio_backend.o" "$out/file_audio_sink.o" \
  -pthread -ldl -lm -o "$out/file-audio-sink-test"
ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/file-audio-sink-test" \
  "$root/artifacts/audio/file-audio-vectors-20260910-a" 2>&1 | tee "$out/sink.log"
for suite in startup cleanup; do
  c++ "${flags[@]}" "$root/scripts/quest/test_audio_backend_${suite}.cpp" "$out/miniaudio_backend.o" \
    -pthread -ldl -lm -o "$out/backend-${suite}-test"
  ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 "$out/backend-${suite}-test" 2>&1 | tee "$out/backend-${suite}.log"
done
python3 "$root/scripts/quest/file_audio_sink_source_manifest.py" "$out/source-after.json" "$out/source-before.json"
printf 'PASS_OUTPUT=%s\n' "$out"
