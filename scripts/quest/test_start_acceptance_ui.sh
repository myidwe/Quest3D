#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${QUEST_BUILD_CACHE:-$HOME/.cache/quest_to_3d-quest-build}"
out="$(mktemp -d "$root/artifacts/quest/stream-retirement/ui-start-acceptance-XXXXXXXX")"
project="$out/project"
mkdir -p "$project"
# Copy current product scripts/resources into a unique project. No shared
# source/.godot cache or native plugin libraries are modified or loaded.
cp "$root/third_party/nightfall/project.godot" "$root/third_party/nightfall/main.gd" "$root/third_party/nightfall/main.gd.uid" "$root/third_party/nightfall/main.tscn" "$project/"
for directory in src test asset models; do cp -a "$root/third_party/nightfall/$directory" "$project/"; done
export XDG_DATA_HOME="$out/user-data"
export XDG_CONFIG_HOME="$out/config"
editor="$cache/linux/Godot_v4.7-stable_linux.x86_64"
"$editor" --headless --editor --xr-mode off --path "$project" --quit > "$out/parse.log" 2>&1
if grep -Eq 'SCRIPT ERROR|Parse Error' "$out/parse.log"; then cat "$out/parse.log"; exit 1; fi
for test_name in test_stream_teardown test_surface_sync_terminal test_autoconnect_race test_reconnect_terminal_order test_app_launch_selection; do
  log="$out/$test_name.log"
  if [[ "$test_name" == test_stream_teardown ]]; then log="$out/teardown.log"; fi
  "$editor" --headless --xr-mode off --path "$project" --script "test/$test_name.gd" > "$log" 2>&1
  cat "$log"
  if grep -Eq 'SCRIPT ERROR|Assertion failed|REGRESSION FAIL|ObjectDB instances were leaked|resources still in use' "$log"; then exit 1; fi
done
printf 'Evidence directory: %s\n' "$out"
