#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
output="$QUEST_ARTIFACTS/codec-retirement-android-20260910"
mkdir -p "$output"
"$ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android29-clang++" \
  -std=c++17 -O2 -Wall -Wextra -Werror -Wno-unused-parameter \
  -I"$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream/src" \
  -c "$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream/src/video/mediacodec_native.cpp" \
  -o "$output/mediacodec_native.o"
"$ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf" \
  -h "$output/mediacodec_native.o"
sha256sum "$output/mediacodec_native.o"
