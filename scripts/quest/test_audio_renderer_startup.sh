#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${QUEST_BUILD_CACHE:-$HOME/.cache/quest_to_3d-quest-build}"
export PATH="$cache/venv/bin:$cache/sysroot/usr/bin:$PATH"
out="$root/artifacts/quest/audio-startup/renderer-build"
if [[ "${1:-}" != "--incremental" ]]; then
  cmake -S "$root/scripts/quest/audio_startup_fixture" -B "$out" -DCMAKE_BUILD_TYPE=Debug \
    -DQUEST_ROOT="$root" -DGODOT_CPP_SOURCE="$cache/native-xr/godot-cpp" \
    -DOPUS_INCLUDE="$cache/vcpkg/packages/opus_arm64-android/include/opus" \
    -DLIMELIGHT_INCLUDE="$cache/vcpkg/buildtrees/moonlight-common-c/src/8df6d3d0d7-3c8d450a9b.clean/src"
fi
cmake --build "$out" -j4
export XDG_DATA_HOME="$root/artifacts/quest/audio-startup/isolated-godot-data"
for suite in startup cleanup; do
  mkdir -p "$out/$suite-project/.godot"
  printf '%s\n' "res://audio_${suite}_probe.gdextension" > "$out/$suite-project/.godot/extension_list.cfg"
  "$cache/linux/Godot_v4.7-stable_linux.x86_64" --headless --xr-mode off \
    --path "$out/$suite-project" --script test.gd 2>&1 | tee "$root/artifacts/quest/audio-startup/renderer-${suite}.log"
  if grep -Eq 'SCRIPT ERROR|Assertion failed|ObjectDB instances were leaked|resources still in use' "$root/artifacts/quest/audio-startup/renderer-${suite}.log"; then exit 1; fi
done
