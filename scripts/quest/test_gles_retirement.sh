#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
out="$QUEST_ARTIFACTS/gles-retirement"
mkdir -p "$out/headers"
# Copy only freestanding public graphics/JNI headers; never add Bionic libc
# headers to a host build and never mutate the pinned shared cache.
headers="$ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/include"
cp -a "$headers/EGL" "$headers/GLES3" "$headers/KHR" "$out/headers/"
cp "$headers/jni.h" "$out/headers/"
c++ -std=c++17 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$QUEST_SCRIPTS/gles_retirement_stubs" -I"$out/headers" \
  -I"$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream/src" \
  "$QUEST_SCRIPTS/test_gles_retirement.cpp" /usr/lib/x86_64-linux-gnu/libEGL.so.1 /usr/lib/x86_64-linux-gnu/libGL.so.1 \
  -ldl -pthread -o "$out/gles-retirement-test"
LIBGL_ALWAYS_SOFTWARE=true GALLIUM_DRIVER=llvmpipe ASAN_OPTIONS=detect_leaks=1 \
  "$out/gles-retirement-test" | tee "$out/test.log"
