#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
export NIGHTFALL_GODOT_EDITOR="$NIGHTFALL_NATIVE_XR_CACHE/godot/bin/godot.linuxbsd.editor.x86_64"
export NIGHTFALL_ANDROID_SOURCE_TEMPLATE="$QUEST_CACHE/godot-templates/templates/android_source.zip"
PROJECT="$QUEST_CACHE/source"
ARTIFACTS="$QUEST_ARTIFACTS"
for file in "$NIGHTFALL_GODOT_EDITOR" "$NIGHTFALL_ANDROID_SOURCE_TEMPLATE" "$PROJECT/addons/nightfall-stream/bin/android/libnightfall-stream.android.template_release.arm64.so"; do
  test -f "$file" || { echo "Required build input missing: $file" >&2; exit 1; }
done
mkdir -p "$XDG_CONFIG_HOME/godot" "$ARTIFACTS"
mkdir -p "$QUEST_TOOLS/signing"
DEBUG_KEYSTORE="$QUEST_TOOLS/signing/debug.keystore"
if [[ ! -f "$DEBUG_KEYSTORE" && -f "$QUEST_CACHE/debug.keystore" ]]; then
  cp "$QUEST_CACHE/debug.keystore" "$DEBUG_KEYSTORE"
fi
if [[ ! -f "$DEBUG_KEYSTORE" ]]; then
  "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$DEBUG_KEYSTORE" \
    -alias androiddebugkey -storepass android -keypass android \
    -dname 'CN=Android Debug,O=Android,C=US' -keyalg RSA -keysize 2048 -validity 10000
fi
# Isolated editor settings: never alter the user's global Godot/Android configuration.
cat > "$XDG_CONFIG_HOME/godot/editor_settings-4.7.tres" <<EOF
[gd_resource type="EditorSettings" format=3]

[resource]
export/android/android_sdk_path = "$ANDROID_HOME"
export/android/java_sdk_path = "$JAVA_HOME"
export/android/debug_keystore = "$DEBUG_KEYSTORE"
export/android/debug_keystore_user = "androiddebugkey"
export/android/debug_keystore_pass = "android"
EOF
cd "$PROJECT"
bash tools/build_support/build_android.sh NightfallDev Quest3DDesktop-debug.apk
cp Quest3DDesktop-debug.apk "$ARTIFACTS/Quest3DDesktop-debug.apk"
sha256sum "$ARTIFACTS/Quest3DDesktop-debug.apk" > "$ARTIFACTS/Quest3DDesktop-debug.apk.sha256"
python3 - "$ARTIFACTS/Quest3DDesktop-debug.apk" <<'PY'
from zipfile import ZipFile
import sys
with ZipFile(sys.argv[1]) as apk:
    files = apk.namelist()
    models = [f for f in files if f.lower().endswith(('.tflite', '.onnx', '.pth', '.pt'))]
    assert not models, f'Unexpected model assets: {models}'
    assert any('libnightfall-stream' in f for f in files), 'Streaming library missing'
    assert any('libnightfall-xr' in f for f in files), 'Native XR library missing'
    godot_lib = apk.read('lib/arm64-v8a/libgodot_android.so')
    assert b'_quest3d_submission_boundary' in godot_lib, 'Patched xrEndFrame observer runtime missing'
    assert b'Cannot unregister an OpenXR provider during frame collection' in godot_lib, 'Observer lifetime guard missing'
    xr_lib = next(apk.read(f) for f in files if 'libnightfall-xr' in f and f.endswith('.so'))
    assert b'get_last_successful_submission' in xr_lib, 'Submission-aware XR renderer missing'
    assert b'query_pointer_candidate' in xr_lib, 'Submitted-frame pointer geometry renderer missing'
    assert b'issue_pointer_ticket' in xr_lib and b'take_pointer_ticket' in xr_lib, 'Immutable pointer ticket API missing'
    stream_lib = next(apk.read(f) for f in files if 'libnightfall-stream' in f and f.endswith('.so'))
    assert b'fetch_pc_frame' in stream_lib and b'request_pc_pointer' in stream_lib, 'Authenticated pointer transport API missing'
    assert b'set_pc_pointer_input_enabled' in stream_lib, 'Explicit native pointer opt-in API missing'
    print('APK native libraries present; no AI model asset files bundled')
PY
echo "APK ready at $ARTIFACTS/Quest3DDesktop-debug.apk (not installed)"
