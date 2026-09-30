#!/usr/bin/env bash
# Run only through the selected MSYS2 UCRT64 msys2_shell.cmd entry point.
# Reads the prepared source and local archives; never starts/replaces a host.
set -euo pipefail
prep="$(cd "$(dirname "$0")" && pwd)"
jobs="${1:-4}"
[[ "$jobs" =~ ^[1-8]$ ]] || { echo 'Jobs must be 1..8' >&2; exit 1; }
source_dir="$prep/source"
build="$prep/cmake-build-historical"
deps="$prep/dependencies"
native_npm="${QUEST3D_NATIVE_NPM:?Set QUEST3D_NATIVE_NPM to the verified native Windows npm.cmd}"
mkdir -p "$build" "$prep/npm-cache" "$prep/temp"
export TMPDIR="$prep/temp"
export npm_config_cache="$prep/npm-cache"
export BRANCH=quest3d
export BUILD_VERSION=2026.906.222525
export COMMIT=cb72dffa3233c5815cd5ba88f09f049dd679ba75-quest3d
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
  -DNPM="$native_npm" \
  -DLIBVIRTUALHID_BUILD_WINDOWS_DRIVER=OFF \
  -DLIBVIRTUALHID_BUILD_WINDOWS_BROKER=OFF -DLIBVIRTUALHID_BUILD_TOOLS=OFF \
  -DFETCHCONTENT_FULLY_DISCONNECTED=ON -DFETCHCONTENT_UPDATES_DISCONNECTED=ON \
  -DFETCHCONTENT_SOURCE_DIR_BOOST="$deps/unpacked/boost-1.89.0" \
  -DFETCHCONTENT_SOURCE_DIR_JSON="$deps/unpacked/json" \
  -DCPM_nv_codec_headers_11_SOURCE="$deps/nv-codec-headers-11" \
  -DCPM_nv_codec_headers_12_SOURCE="$deps/nv-codec-headers-12" \
  -DCPM_nv_codec_headers_13_SOURCE="$deps/nv-codec-headers-13" \
  -DFFMPEG_PREPARED_BINARIES="$deps/unpacked/ffmpeg"
cmake --build "$build" --parallel "$jobs" --target sunshine test_sunshine
cd "$prep"
"$build/tests/test_sunshine.exe" \
  --gtest_filter='Quest3DFrameLedger.*:QuestAudioPacketGate.*:QuestAudioQueue.*:QuestAudioDelayTest.*:Quest3DControl*' \
  --gtest_output=xml:historical-regression.xml
