#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${QUEST_BUILD_CACHE:-$HOME/.cache/quest_to_3d-quest-build}"
out="$root/artifacts/quest/xr-retirement/android-objects"
cpp="$cache/native-xr/godot-cpp"
compiler="$cache/android-sdk/ndk/29.0.14206865/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android29-clang++"
mkdir -p "$out"
for unit in fast_xr_renderer fast_xr_renderer_android fast_xr_renderer_pointer; do
  "$compiler" -std=c++17 -O2 -fPIC -Wall -Wextra -Werror -Wno-unused-parameter -Wno-missing-field-initializers \
    -I"$cpp/include" -I"$cpp/gen/include" -I"$cpp/gdextension" \
    -I"$cache/native-xr/godot/thirdparty/openxr/include" \
    -I"$root/third_party/nightfall/extensions/nightfall-xr/include" \
    -c "$root/third_party/nightfall/extensions/nightfall-xr/src/$unit.cpp" -o "$out/$unit.o"
done
"$compiler" --version > "$out/compiler.txt"
sha256sum "$out/"*.o | tee "$out/SHA256SUMS"
