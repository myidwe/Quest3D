#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${QUEST_BUILD_CACHE:-$HOME/.cache/quest_to_3d-quest-build}"
mode="${1:-fixed}"
case "$mode" in baseline|fixed) ;; *) exit 2 ;; esac
mkdir -p "$root/artifacts/quest/menu-quality"
out="$(mktemp -d "$root/artifacts/quest/menu-quality/$mode-XXXXXXXX")"
project="$out/project"
mkdir -p "$project"
cp "$root/third_party/nightfall/project.godot" "$root/third_party/nightfall/main.gd" "$root/third_party/nightfall/main.gd.uid" "$root/third_party/nightfall/main.tscn" "$project/"
for directory in src test asset models; do cp -a "$root/third_party/nightfall/$directory" "$project/"; done
cp "$root/scripts/quest/stream_resolution_visibility.gd" "$project/test/"
sha256sum "$project/main.gd" "$project/src/ui_controller.gd" "$project/src/performance_telemetry.gd" "$project/src/menu_schema.gd" "$project/src/settings_controller.gd" "$project/src/screen_manager.gd" "$project/src/stream_manager.gd" "$project/test/stream_resolution_visibility.gd" "$project/test/test_stream_resolution.gd" "$project/test/test_screen_presentation_controls.gd" > "$out/source-sha256.txt"
export XDG_DATA_HOME="$out/user-data" XDG_CONFIG_HOME="$out/config"
export LIBGL_ALWAYS_SOFTWARE=1
editor="$cache/linux/Godot_v4.7-stable_linux.x86_64"
sha256sum "$editor" > "$out/godot-sha256.txt"
timeout 120 "$editor" --headless --editor --xr-mode off --path "$project" --quit > "$out/parse.log" 2>&1
if grep -Eq 'SCRIPT ERROR|Parse Error' "$out/parse.log"; then cat "$out/parse.log"; exit 1; fi
timeout 60 xvfb-run -a --server-args='-screen 0 1280x720x24' "$editor" --display-driver x11 --rendering-method gl_compatibility --rendering-driver opengl3 --audio-driver Dummy --xr-mode off --path "$project" --script test/stream_resolution_visibility.gd -- "$out" "$mode" > "$out/render.log" 2>&1
cat "$out/render.log"
if grep -Eq '^ERROR:|SCRIPT ERROR|Assertion failed|ObjectDB instances were leaked|resources still in use' "$out/render.log"; then exit 1; fi
if [[ "$mode" == fixed ]]; then
  for test in test_stream_resolution test_menu_move_handle test_performance_telemetry test_screen_presentation_controls; do
    timeout 45 "$editor" --headless --xr-mode off --path "$project" --script "test/$test.gd" > "$out/$test.log" 2>&1
    cat "$out/$test.log"
    if grep -Eq '^ERROR:|SCRIPT ERROR|Assertion failed|ObjectDB instances were leaked|resources still in use' "$out/$test.log"; then exit 1; fi
  done
fi
printf 'Private evidence: %s\n' "$out"
