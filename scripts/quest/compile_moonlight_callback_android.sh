#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
out="$QUEST_ARTIFACTS/moonlight-callback-drain"
src="$out/source"
"$ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android29-clang" \
  -std=c11 -O2 -Wall -Wextra -Werror -Wno-unused-parameter -DHAS_SOCKLEN_T -DLC_DEBUG \
  -I"$src/src" -I"$src/enet/include" \
  -I"$QUEST_CACHE/vcpkg/packages/openssl_arm64-android/include" \
  -c "$src/src/Connection.c" -o "$out/Connection.android.o"
"$ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf" -h "$out/Connection.android.o"
sha256sum "$out/Connection.android.o"
