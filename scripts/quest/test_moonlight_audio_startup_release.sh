#!/usr/bin/env bash
set -euo pipefail
export QUEST_AUDIO_TEST_BUILD_TYPE=Release
exec bash "$(dirname "$0")/test_moonlight_audio_startup.sh" "$@"
