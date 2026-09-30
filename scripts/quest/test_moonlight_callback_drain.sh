#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${QUEST_BUILD_CACHE:-$HOME/.cache/quest_to_3d-quest-build}"
export PATH="$cache/venv/bin:$cache/sysroot/usr/bin:$PATH"
out="$root/artifacts/quest/moonlight-callback-drain"
python3 "$root/scripts/quest/prepare_moonlight_callback_drain.py"
cmake -S "$root/scripts/quest/moonlight_callback_fixture" -B "$out/build" \
  -DMOONLIGHT_SOURCE="$out/source" \
  -DOPENSSL_INCLUDE_DIR="$cache/sysroot/usr/include" \
  -DOPENSSL_CRYPTO_LIBRARY=/usr/lib/x86_64-linux-gnu/libcrypto.so.3 \
  -DHOST_MULTIARCH_INCLUDE="$cache/sysroot/usr/include/x86_64-linux-gnu"
cmake --build "$out/build" -j4
ASAN_OPTIONS=detect_leaks=1 "$out/build/callback-test" 2>&1 | tee "$out/test.log"
ASAN_OPTIONS=detect_leaks=1 "$out/build/callback-test" create-failure 2>&1 | tee -a "$out/test.log"
ASAN_OPTIONS=detect_leaks=1 "$out/build/callback-test" join-failure sticky 2>&1 | tee -a "$out/test.log"
