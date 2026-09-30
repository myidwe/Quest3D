#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${QUEST_BUILD_CACHE:-$HOME/.cache/quest_to_3d-quest-build}"
export PATH="$cache/venv/bin:$cache/sysroot/usr/bin:$PATH"
out="$root/artifacts/quest/stream-retirement/build"
sanitize="${STREAM_RETIREMENT_SANITIZE:-OFF}"
test_script="${STREAM_RETIREMENT_TEST_SCRIPT:-test.gd}"
case "$test_script" in test.gd|test_start_refusal.gd) ;; *) exit 2 ;; esac
if [[ "$sanitize" == ON ]]; then out="$out-asan"; fi
mkdir -p "$out"
cmake -S "$root/scripts/quest/stream_retirement_fixture" -B "$out" -DCMAKE_BUILD_TYPE=Debug \
  -DSTREAM_RETIREMENT_SANITIZE="$sanitize" \
  -DQUEST_ROOT="$root" -DGODOT_CPP_SOURCE="$cache/native-xr/godot-cpp" \
  -DOPUS_INCLUDE="$cache/vcpkg/packages/opus_arm64-android/include/opus" \
  -DFFMPEG_INCLUDE="$cache/vcpkg/packages/ffmpeg_arm64-android/include" \
  -DLIMELIGHT_INCLUDE="$cache/vcpkg/buildtrees/moonlight-common-c/src/8df6d3d0d7-3c8d450a9b.clean/src"
cmake --build "$out" -j4
export XDG_DATA_HOME="$root/artifacts/quest/stream-retirement/isolated-data"
mkdir -p "$out/project/.godot"
printf '%s\n' 'res://stream_retirement_probe.gdextension' > "$out/project/.godot/extension_list.cfg"
log="$root/artifacts/quest/stream-retirement/runtime.log"
if [[ "$test_script" == test_start_refusal.gd ]]; then log="$root/artifacts/quest/stream-retirement/runtime-start-refusal.log"; fi
if [[ "$sanitize" == ON ]]; then
  export LD_PRELOAD="$(g++ -print-file-name=libasan.so)"
  export ASAN_OPTIONS=detect_leaks=1:halt_on_error=1
  export UBSAN_OPTIONS=halt_on_error=1
  log="$root/artifacts/quest/stream-retirement/runtime-asan.log"
fi
"$cache/linux/Godot_v4.7-stable_linux.x86_64" --headless --xr-mode off --path "$out/project" --script "$test_script" 2>&1 | tee "$log"
if grep -Eq 'SCRIPT ERROR|Assertion failed|ObjectDB instances were leaked|resources still in use|ERROR: AddressSanitizer|runtime error:|LeakSanitizer' "$log"; then exit 1; fi
