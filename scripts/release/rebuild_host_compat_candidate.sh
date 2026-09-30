#!/usr/bin/env bash
# 격리 API 호환 후보만 offline 빌드 · 제품 실행/교체 없음
set -euo pipefail
prep="$(cd "$(dirname "$0")" && pwd)"
jobs="${1:-4}"
[[ "$jobs" =~ ^[1-8]$ ]] || { echo 'Jobs must be 1..8' >&2; exit 1; }
source_dir="$prep/source"
build="$prep/cmake-build-compat-candidate"
deps="${QUEST3D_COMPAT_DEPS:?Set read-only shared historical dependency path}"
native_npm="${QUEST3D_NATIVE_NPM:?Set verified native Windows npm.cmd}"
phase=configure
native_build_success=false
tests_passed=false
started_seconds="$SECONDS"
write_build_status() {
  result="$?"
  trap - EXIT
  exe_produced=false
  exe_sha=""
  if [[ -f "$build/sunshine.exe" ]]; then
    exe_produced=true
    exe_sha="$(sha256sum "$build/sunshine.exe" | cut -d ' ' -f 1)"
  fi
  printf '{"schema":1,"kind":"native-api-compat-candidate-build","exit_code":%s,"last_phase":"%s","elapsed_seconds":%s,"native_build_success":%s,"tests_passed":%s,"host_exe_produced":%s,"host_exe_sha256":"%s","historical_binary_source_correspondence_verified":false,"release_source_gate":false,"runtime_replaced":false,"server_started":false,"hardware_validation":false}\n' \
    "$result" "$phase" "$((SECONDS-started_seconds))" "$native_build_success" "$tests_passed" "$exe_produced" "$exe_sha" \
    > "$prep/candidate-build-summary.json"
  exit "$result"
}
trap write_build_status EXIT
mkdir -p "$build" "$prep/npm-cache" "$prep/temp"
export TMPDIR="$prep/temp"
export npm_config_cache="$prep/npm-cache"
export BRANCH=quest3d-api-compat-candidate
export BUILD_VERSION=2026.930.1
export COMMIT=cb72dffa3233c5815cd5ba88f09f049dd679ba75-api-compat-candidate
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.excludesFile
export GIT_CONFIG_VALUE_0="$prep/empty-git-ignore"
touch "$GIT_CONFIG_VALUE_0"
pacman -Q > "$prep/msys2-packages.actual.txt"
g++ --version > "$prep/compiler.version.txt"
cmake --version > "$prep/cmake.version.txt"
cmake -S "$source_dir" -B "$build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_DOCS=OFF -DBUILD_TESTS=ON \
  -DSUNSHINE_ENABLE_TRAY=OFF -DSUNSHINE_USE_STATIC_QT=OFF \
  -DSUNSHINE_ENABLE_WIX=OFF -DSUNSHINE_PUBLISHER_NAME=Quest3D \
  -DNPM="$native_npm" -DNPM_OFFLINE=ON \
  -DLIBVIRTUALHID_BUILD_WINDOWS_DRIVER=OFF \
  -DLIBVIRTUALHID_BUILD_WINDOWS_BROKER=OFF -DLIBVIRTUALHID_BUILD_TOOLS=OFF \
  -DFETCHCONTENT_FULLY_DISCONNECTED=ON -DFETCHCONTENT_UPDATES_DISCONNECTED=ON \
  -DFETCHCONTENT_SOURCE_DIR_BOOST="$deps/unpacked/boost-1.89.0" \
  -DFETCHCONTENT_SOURCE_DIR_JSON="$deps/unpacked/json" \
  -DCPM_nv_codec_headers_11_SOURCE="$deps/nv-codec-headers-11" \
  -DCPM_nv_codec_headers_12_SOURCE="$deps/nv-codec-headers-12" \
  -DCPM_nv_codec_headers_13_SOURCE="$deps/nv-codec-headers-13" \
  -DFFMPEG_PREPARED_BINARIES="$deps/unpacked/ffmpeg"
phase=build
cmake --build "$build" --parallel "$jobs" --target sunshine test_sunshine
native_build_success=true
phase=tests
cd "$prep"
"$build/tests/test_sunshine.exe" \
  --gtest_filter='QuestAudioPacketCompat.*:Quest3DFrameLedger.*:QuestAudioPacketGate.*:QuestAudioQueue.*:QuestAudioDelayTest.*:Quest3DControl*' \
  --gtest_output=xml:candidate-regression.xml
tests_passed=true
