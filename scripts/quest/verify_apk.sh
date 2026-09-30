#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
ARTIFACTS="$QUEST_ARTIFACTS"
APK="$ARTIFACTS/Quest3DDesktop-debug.apk"
"$ANDROID_HOME/build-tools/36.1.0/apksigner" verify --verbose --print-certs "$APK" > "$ARTIFACTS/apk-signature.txt"
"$ANDROID_HOME/build-tools/36.1.0/aapt2" dump badging "$APK" > "$ARTIFACTS/apk-badging.txt"
cat "$ARTIFACTS/apk-signature.txt"
head -n 6 "$ARTIFACTS/apk-badging.txt"
