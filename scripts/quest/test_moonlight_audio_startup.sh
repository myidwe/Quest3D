#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${QUEST_BUILD_CACHE:-$HOME/.cache/quest_to_3d-quest-build}"
export PATH="$cache/venv/bin:$cache/sysroot/usr/bin:$PATH"
out="$root/artifacts/quest/moonlight-audio-startup"
python3 "$root/scripts/quest/prepare_moonlight_audio_startup.py" "$@"
build_type="${QUEST_AUDIO_TEST_BUILD_TYPE:-Debug}"
build_dir="$out/build"
log_name=test.log
if [[ "$build_type" == Release ]]; then build_dir="$out/build-release"; log_name=test-release.log; fi
cmake -S "$root/scripts/quest/moonlight_audio_startup_fixture" -B "$build_dir" \
  -DCMAKE_BUILD_TYPE="$build_type" \
  -DMOONLIGHT_SOURCE="$out/source" \
  -DOPENSSL_INCLUDE_DIR="$cache/sysroot/usr/include" \
  -DOPENSSL_CRYPTO_LIBRARY=/usr/lib/x86_64-linux-gnu/libcrypto.so.3 \
  -DOPUS_INCLUDE_DIR="$cache/vcpkg/buildtrees/opus/src/v1.5.2-81ed242155.clean/include" \
  -DHOST_MULTIARCH_INCLUDE="$cache/sysroot/usr/include/x86_64-linux-gnu"
cmake --build "$build_dir" -j4
ASAN_OPTIONS=detect_leaks=1 "$build_dir/audio-startup-test" "$out/trace-$build_type-fixture.jsonl" 2>&1 | tee "$out/$log_name"
ASAN_OPTIONS=detect_leaks=1 "$build_dir/audio-startup-production-denial-test" "$out/trace-$build_type-production.jsonl" 2>&1 | tee -a "$out/$log_name"
