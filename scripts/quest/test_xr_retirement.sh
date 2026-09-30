#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${QUEST_BUILD_CACHE:-$HOME/.cache/quest_to_3d-quest-build}"
export PATH="$cache/venv/bin:$cache/sysroot/usr/bin:$PATH"
out="$root/artifacts/quest/xr-retirement/fixture-build"
headers="$out/android-headers"
ndk="$cache/android-sdk/ndk/29.0.14206865/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/include"
mkdir -p "$headers/android"
# Isolate standalone graphics/JNI declarations from Android's Bionic headers.
if [[ ! -f "$out/CMakeCache.txt" ]]; then
  cp -R "$ndk/EGL" "$ndk/GLES2" "$ndk/GLES3" "$ndk/KHR" "$headers/"
  cp "$ndk/jni.h" "$headers/"
  cp "$root/scripts/quest/codec_test_stubs/android/log.h" "$headers/android/"
  cmake -S "$root/scripts/quest/xr_retirement_fixture" -B "$out" -DCMAKE_BUILD_TYPE=Debug \
  -DQUEST_ROOT="$root" -DGODOT_CPP_SOURCE="$cache/native-xr/godot-cpp" \
  -DFIXTURE_HEADERS="$headers" -DOPENXR_INCLUDE="$cache/native-xr/godot/thirdparty/openxr/include"
fi
cmake --build "$out" -j4
export XDG_DATA_HOME="$root/artifacts/quest/xr-retirement/isolated-godot-data"
export XDG_CONFIG_HOME="$root/artifacts/quest/xr-retirement/isolated-godot-config"
export LIBGL_ALWAYS_SOFTWARE=true GALLIUM_DRIVER=llvmpipe
mkdir -p "$out/project/.godot"
printf '%s\n' 'res://xr_retirement_probe.gdextension' > "$out/project/.godot/extension_list.cfg"
mkdir -p "$out/project/src"
for source in native_xr_renderer pc_color_probe picture_color pc_pointer_probe; do
  cp "$root/third_party/nightfall/src/$source.gd" "$out/project/src/"
done
cp "$root/scripts/quest/xr_retirement_fixture/vr_screen.gd" "$root/scripts/quest/xr_retirement_fixture/host_settings.gd" "$out/project/src/"
# Register script class names for the full production manager script. Only its
# scene type dependencies are small explicit stand-ins in this isolated project.
"$cache/linux/Godot_v4.7-stable_linux.x86_64" --headless --editor --xr-mode off --path "$out/project" --quit > "$out/script-import.log" 2>&1
if grep -Eq 'SCRIPT ERROR|Parse Error' "$out/script-import.log"; then cat "$out/script-import.log"; exit 1; fi
log="$root/artifacts/quest/xr-retirement/${1:-regression}.log"
"$cache/linux/Godot_v4.7-stable_linux.x86_64" --headless --xr-mode off --path "$out/project" --script test.gd -- "${@}" 2>&1 | tee "$log"
if grep -Eq 'SCRIPT ERROR|Assertion failed|ObjectDB instances were leaked|resources still in use' "$log"; then exit 1; fi
