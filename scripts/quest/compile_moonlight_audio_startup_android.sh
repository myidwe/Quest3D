#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
out="$QUEST_ARTIFACTS/moonlight-audio-startup"
src="$out/source"
mkdir -p "$out/android-objects"
for unit in AudioStream RtpAudioQueue Connection; do
  "$ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android29-clang" \
    -std=c11 -O2 -Wall -Wextra -Werror -Wno-unused-parameter -DHAS_SOCKLEN_T -DLC_DEBUG \
    -I"$src/src" -I"$src/enet/include" \
    -I"$QUEST_CACHE/vcpkg/packages/openssl_arm64-android/include" \
    -c "$src/src/$unit.c" -o "$out/android-objects/$unit.android.o"
done
sha256sum "$out"/android-objects/*.o | tee "$out/android-objects/SHA256SUMS"
