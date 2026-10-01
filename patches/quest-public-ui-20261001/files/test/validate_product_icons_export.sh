#!/usr/bin/env bash
# Verify Godot's exported resource remap, not only editor source files.
set -euo pipefail
[[ $# -eq 2 ]] || { echo 'Usage: validate_product_icons_export.sh GODOT NEW_OUTPUT' >&2; exit 2; }
EDITOR="$1"
OUT="$2"
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
test -x "$EDITOR"
test ! -e "$OUT"
mkdir -p "$OUT/project/src/assets/precision" "$OUT/project/test"
cp "$PROJECT/src/product_theme.gd" "$OUT/project/src/"
cp -R "$PROJECT/src/assets/precision/icons" "$PROJECT/src/assets/precision/fonts" "$OUT/project/src/assets/precision/"
cp "$PROJECT/test/test_product_icons.gd" "$OUT/project/test/"
cat > "$OUT/project/project.godot" <<'EOF'
config_version=5
[application]
config/name="Quest3D Icon Export Fixture"
[rendering]
renderer/rendering_method="gl_compatibility"
EOF
cat > "$OUT/project/export_presets.cfg" <<'EOF'
[preset.0]
name="IconFixture"
platform="Linux/X11"
runnable=true
export_filter="all_resources"
include_filter=""
exclude_filter=""
script_export_mode=2
[preset.0.options]
binary_format/architecture="x86_64"
EOF
export XDG_DATA_HOME="$OUT/profile/data" XDG_CONFIG_HOME="$OUT/profile/config" XDG_CACHE_HOME="$OUT/profile/cache"
mkdir -p "$XDG_DATA_HOME" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME"
"$EDITOR" --headless --xr-mode off --editor --path "$OUT/project" --import --quit > "$OUT/import.log" 2>&1
"$EDITOR" --headless --xr-mode off --path "$OUT/project" --export-pack IconFixture "$OUT/icons.pck" > "$OUT/export.log" 2>&1
"$EDITOR" --headless --audio-driver Dummy --xr-mode off --main-pack "$OUT/icons.pck" --script res://test/test_product_icons.gd > "$OUT/run.log" 2>&1
grep -F 'PRODUCT_ICONS 86 checks, 0 failures; raw SVG present=false' "$OUT/run.log"
! grep -E 'SCRIPT ERROR:|Parse Error:|Failed to load script' "$OUT/import.log" "$OUT/export.log" "$OUT/run.log"
