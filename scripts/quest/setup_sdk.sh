#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
# sdkmanager reads these license acceptances from stdin; isolated SDK only.
yes | sdkmanager --sdk_root="$ANDROID_HOME" --licenses || test "${PIPESTATUS[1]}" = 0
sdkmanager --sdk_root="$ANDROID_HOME" 'platform-tools' 'platforms;android-36' 'build-tools;36.1.0'
echo 'Android SDK ready'
