#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$QUEST_CACHE/codec-retirement-tests"
# Android and desktop JDK differ in the C++ AttachCurrentThread overload.
# Use the pinned NDK's standalone JNI header without adding its Bionic headers
# to a host compilation's include path.
cp "$ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/include/jni.h" \
  "$QUEST_CACHE/codec-retirement-tests/jni.h"
g++ -std=c++17 -O1 -g -Wall -Wextra -Werror -pthread -D__ANDROID__ \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$QUEST_SCRIPTS/codec_test_stubs" \
  -I"$QUEST_CACHE/codec-retirement-tests" \
  -I"$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream/src" \
  "$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream/src/video/mediacodec_native.cpp" \
  "$QUEST_SCRIPTS/test_codec_retirement.cpp" -ldl \
  -o "$QUEST_CACHE/codec-retirement-tests/codec-retirement"
"$QUEST_CACHE/codec-retirement-tests/codec-retirement"
