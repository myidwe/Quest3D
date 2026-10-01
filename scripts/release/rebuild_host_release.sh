#!/usr/bin/env bash
# Build/test the exact supplied baseline; never install or start a host.
set -euo pipefail
workspace="$(cd "${1:?Set the unpacked new build workspace}" && pwd)"
jobs="${2:-6}"
[[ "$jobs" =~ ^[1-8]$ ]] || { echo 'Jobs must be 1..8' >&2; exit 1; }
source_dir="$workspace/source"
build="$workspace/cmake-build-release"
deps="$workspace/dependencies"
native_npm="$(cygpath -m "${QUEST3D_NATIVE_NPM:?Set locked native Windows npm.cmd path}")"
[[ -f "$workspace/unpack-verification.json" ]] || { echo 'Run the verified source unpacker first' >&2; exit 1; }
mkdir -p "$workspace/temp" "$workspace/npm-cache"
export TMPDIR="$workspace/temp" npm_config_cache="$workspace/npm-cache"
export BRANCH=quest3d-api-compat-candidate BUILD_VERSION=2026.930.1
export COMMIT=cb72dffa3233c5815cd5ba88f09f049dd679ba75-api-compat-candidate
unset CODECOV_TOKEN GITHUB_REPOSITORY
cmake -S "$source_dir" -B "$build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_DOCS=OFF -DBUILD_TESTS=ON \
  -DSUNSHINE_ENABLE_TRAY=OFF -DSUNSHINE_USE_STATIC_QT=OFF \
  -DSUNSHINE_ENABLE_WIX=OFF -DSUNSHINE_PUBLISHER_NAME=Quest3D \
  -DNPM="$native_npm" -DNPM_OFFLINE=ON \
  -DLIBVIRTUALHID_BUILD_WINDOWS_DRIVER=OFF -DLIBVIRTUALHID_BUILD_WINDOWS_BROKER=OFF -DLIBVIRTUALHID_BUILD_TOOLS=OFF \
  -DFETCHCONTENT_FULLY_DISCONNECTED=ON -DFETCHCONTENT_UPDATES_DISCONNECTED=ON \
  -DFETCHCONTENT_SOURCE_DIR_BOOST="$deps/unpacked/boost-1.89.0" \
  -DFETCHCONTENT_SOURCE_DIR_JSON="$deps/unpacked/json" \
  -DCPM_nv_codec_headers_11_SOURCE="$deps/nv-codec-headers-11" \
  -DCPM_nv_codec_headers_12_SOURCE="$deps/nv-codec-headers-12" \
  -DCPM_nv_codec_headers_13_SOURCE="$deps/nv-codec-headers-13" \
  -DFFMPEG_PREPARED_BINARIES="$deps/unpacked/ffmpeg"
cmake --build "$build" --parallel "$jobs" --target sunshine test_sunshine
( cd "$workspace"; "$build/tests/test_sunshine.exe" \
  --gtest_filter='QuestAudioPacketCompat.*:Quest3DFrameLedger.*:QuestAudioPacketGate.*:QuestAudioQueue.*:QuestAudioDelayTest.*:Quest3DControl*' \
  --gtest_output=xml:release-regression.xml )
( cd "$source_dir"; cmd.exe /d /c "$native_npm" ci --ignore-scripts --no-audit --no-fund )
cmake --build "$build" --parallel "$jobs" --target web-ui
