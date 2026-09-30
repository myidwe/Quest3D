#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${QUEST_BUILD_CACHE:-$HOME/.cache/quest_to_3d-quest-build}"
mkdir -p "$root/artifacts/quest/stream-resolution"
out="$(mktemp -d "$root/artifacts/quest/stream-resolution/observation-XXXXXXXX")"
mode="${1:-all}"
case "$mode" in all|ui) ;; *) exit 2 ;; esac
ndk="$cache/android-sdk/ndk/29.0.14206865/toolchains/llvm/prebuilt/linux-x86_64"
src="$root/third_party/nightfall/addons/nightfall-stream/src"
if [[ "$mode" == all ]]; then
cp "$ndk/sysroot/usr/include/jni.h" "$out/jni.h"
g++ -std=c++17 -O1 -g -Wall -Wextra -Werror -pthread -D__ANDROID__ \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$root/scripts/quest/codec_test_stubs" -I"$out" -I"$src" \
  "$src/video/mediacodec_native.cpp" "$root/scripts/quest/test_codec_retirement.cpp" -ldl \
  -o "$out/codec-observation"
ASAN_OPTIONS=detect_leaks=1:halt_on_error=1 UBSAN_OPTIONS=halt_on_error=1 \
  "$out/codec-observation" > "$out/codec.log" 2>&1
cat "$out/codec.log"
"$ndk/bin/aarch64-linux-android29-clang++" -std=c++17 -Wall -Wextra -Werror \
  -I"$src" -c "$src/video/mediacodec_native.cpp" -o "$out/mediacodec_native.arm64.o"
fi
project="$out/project"
mkdir -p "$project"
cp "$root/third_party/nightfall/project.godot" "$root/third_party/nightfall/main.gd" "$root/third_party/nightfall/main.gd.uid" "$root/third_party/nightfall/main.tscn" "$project/"
for directory in src test asset models; do cp -a "$root/third_party/nightfall/$directory" "$project/"; done
export XDG_DATA_HOME="$out/user-data"
export XDG_CONFIG_HOME="$out/config"
editor="$cache/linux/Godot_v4.7-stable_linux.x86_64"
timeout 120 "$editor" --headless --editor --xr-mode off --path "$project" --quit > "$out/parse.log" 2>&1
if grep -Eq 'SCRIPT ERROR|Parse Error' "$out/parse.log"; then cat "$out/parse.log"; exit 1; fi
for test in test_stream_resolution test_menu_move_handle test_performance_telemetry; do
  timeout 45 "$editor" --headless --xr-mode off --path "$project" --script "test/$test.gd" > "$out/$test.log" 2>&1
  cat "$out/$test.log"
  if grep -Eq 'SCRIPT ERROR|Assertion failed|ObjectDB instances were leaked|resources still in use' "$out/$test.log"; then exit 1; fi
done
printf 'Private evidence directory: %s\n' "$out"
