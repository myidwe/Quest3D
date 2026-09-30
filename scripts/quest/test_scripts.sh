#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
export NIGHTFALL_GODOT_EDITOR="$QUEST_CACHE/linux/Godot_v4.7-stable_linux.x86_64"
export NIGHTFALL_TEST_DATA_ROOT="$QUEST_ARTIFACTS/gdscript-tests"
cd "$QUEST_CACHE/source"
bash test/check_gdscript_parse.sh
bash test/run_gdscript_tests.sh
for test_name in test_pc_full_sbs test_pc_control test_pc_pointer test_pc_pointer_control test_pairing_lifecycle test_app_selection test_app_launch_selection test_xr_display_cadence test_screen_presentation_controls test_pc_profile_bitrate test_menu_move_handle test_pc_color_probe test_stream_teardown test_picture_color test_stream_resolution; do
  "$NIGHTFALL_GODOT_EDITOR" --headless --xr-mode off --path . --script "test/$test_name.gd" 2>&1 | tee "$NIGHTFALL_TEST_DATA_ROOT/$test_name.out"
  if grep -Eq 'SCRIPT ERROR:|Assertion failed' "$NIGHTFALL_TEST_DATA_ROOT/$test_name.out"; then exit 1; fi
done
bash "$QUEST_SCRIPTS/test_codec_caps.sh"
