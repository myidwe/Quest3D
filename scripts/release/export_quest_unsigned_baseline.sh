#!/usr/bin/env bash
# Fresh project/Java/DEX packaging. No key generation, signing, or installation.
set -euo pipefail
[[ $# -ge 3 && $# -le 4 ]] || { echo 'Usage: export_quest_unsigned_baseline.sh PREPARED_EXPORT REBUILT_ENGINE PINNED_CACHE [ATTEMPT]' >&2; exit 2; }
EXPORT="$(cd "$1" && pwd -P)"
ENGINE="$(cd "$2" && pwd -P)"
TOOLS="$(cd "$3" && pwd -P)"
ATTEMPT="${4:-first}"
[[ "$ATTEMPT" =~ ^[a-z][a-z0-9-]{0,31}$ ]] || exit 2
REPORT_PREFIX="apk-export-$ATTEMPT"
test -f "$EXPORT/android-export-preparation.json"
test -f "$ENGINE/engine-build-summary.json"
test ! -e "$EXPORT/$REPORT_PREFIX.started"
test ! -e "$EXPORT/$REPORT_PREFIX-summary.json"
test ! -e "$EXPORT/Quest3D-public-review-unsigned.apk"
[[ "$EXPORT" == /mnt/a/* ]] || exit 2
python3 - "$ENGINE" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]); m=json.loads((p/'engine-build-summary.json').read_text())
assert m['exit_code']==0 and m['clean_android_engine_build_verified'] is True
for name in ('libgodot_android.so','libc++_shared.so'):
    f=p/'runtime'/name
    assert hashlib.sha256(f.read_bytes()).hexdigest()==m['outputs'][name]['sha256']
PY
test -d "$EXPORT/cache/gradle/caches/modules-2"
test -f "$EXPORT/dependency-audit-standard-release-summary.json"
python3 - "$EXPORT/dependency-audit-standard-release-summary.json" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))
assert m['exit_code']==0 and m['resolved'] is True
PY
touch "$EXPORT/$REPORT_PREFIX.started"
export JAVA_HOME="$TOOLS/linux/jdk-17.0.20.1+1"
export ANDROID_HOME="$TOOLS/android-sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export GRADLE_USER_HOME="$EXPORT/cache/gradle"
export TMPDIR="$EXPORT/tmp"
export XDG_DATA_HOME="$EXPORT/cache/xdg-data"
export XDG_CONFIG_HOME="$EXPORT/cache/xdg-config"
export XDG_CACHE_HOME="$EXPORT/cache/xdg-cache"
export PATH="$TOOLS/venv/bin:$TOOLS/sysroot/usr/bin:$JAVA_HOME/bin:$PATH"
export NIGHTFALL_INSTALL=0
mkdir -p "$TMPDIR" "$XDG_CONFIG_HOME/godot" "$XDG_DATA_HOME" "$XDG_CACHE_HOME"
cat > "$XDG_CONFIG_HOME/godot/editor_settings-4.7.tres" <<EOF
[gd_resource type="EditorSettings" format=3]
[resource]
export/android/android_sdk_path = "$ANDROID_HOME"
export/android/java_sdk_path = "$JAVA_HOME"
export/android/shutdown_adb_on_exit = false
EOF
START="$(date +%s)"
PHASE=inject-engine
trap 'status=$?; python3 - "$EXPORT" "$PHASE" "$status" "$START" "$REPORT_PREFIX" <<"PY"
import hashlib,json,pathlib,sys,time,zipfile
p=pathlib.Path(sys.argv[1]); apk=p/"Quest3D-public-review-unsigned.apk"
result={"schema":1,"phase":sys.argv[2],"exit_code":int(sys.argv[3]),"elapsed_seconds":int(time.time())-int(sys.argv[4]),"full_project_export_verified":int(sys.argv[3])==0,"signed":False,"installed":False,"published":False,"public_release_ready":False,"clean_editor_build_verified":False,"godot_java_aar_rebuilt_from_source":False,"vendor_built_from_source":False,"static_dependencies_rebuilt_from_source":False,"dependency_notices_verified":False}
prep=json.loads((p/"android-export-preparation.json").read_text())
result["vendor_built_from_source"]=prep.get("vendor_built_from_source",False)
result["public_vendor_binding"]=prep.get("public_vendor_binding")
if apk.is_file():
    result["apk"]={"sha256":hashlib.sha256(apk.read_bytes()).hexdigest(),"bytes":apk.stat().st_size}
    with zipfile.ZipFile(apk) as z:
        result["native"]={n:{"sha256":hashlib.sha256(z.read(n)).hexdigest(),"bytes":z.getinfo(n).file_size} for n in z.namelist() if n.startswith("lib/") and n.endswith(".so")}
        result["dex"]={n:{"sha256":hashlib.sha256(z.read(n)).hexdigest(),"bytes":z.getinfo(n).file_size} for n in z.namelist() if n.endswith(".dex")}
with (p/(sys.argv[5]+"-summary.json")).open("x") as f: json.dump(result,f,indent=2); f.write("\n")
PY
exit "$status"' EXIT
exec > >(tee "$EXPORT/$REPORT_PREFIX-private.log") 2>&1
PROJECT="$EXPORT/project"
python3 "$PROJECT/tools/build_support/inject_android_runtime.py" \
  "$PROJECT/android/build/libs/release/godot-lib.template_release.aar" \
  "$ENGINE/runtime/libgodot_android.so" "$ENGINE/runtime/libc++_shared.so"
PHASE=android-manifest
python3 - "$PROJECT/android/build/src/main/AndroidManifest.xml" <<'PY'
import pathlib,sys
p=pathlib.Path(sys.argv[1]); text=p.read_text()
needle='tools:targetApi="29" />'
assert text.count(needle)==1
meta='<meta-data android:name="com.oculus.trade_cpu_for_gpu_amount" android:value="1" />'
assert text.count('android:name="com.oculus.trade_cpu_for_gpu_amount"') in (0,1)
if meta not in text:
    text=text.replace(needle,needle+'\n        '+meta)
p.write_text(text)
PY
PHASE=project-import
EDITOR="$TOOLS/native-xr/godot/bin/godot.linuxbsd.editor.x86_64"
"$EDITOR" --headless --editor --path "$PROJECT" --import
PHASE=unsigned-android-export
# Gradle artifacts were resolved offline in the private A cache first. The
# normal Godot exporter supplies its pinned package/version/ABI properties.
"$EDITOR" --headless --path "$PROJECT" --export-release Quest3DPublicReview "$EXPORT/Quest3D-public-review-unsigned.apk"
test -f "$EXPORT/Quest3D-public-review-unsigned.apk"
PHASE=export-validation
python3 - "$EXPORT" "$REPORT_PREFIX" <<'PY'
import json,pathlib,re,subprocess,sys,zipfile
p=pathlib.Path(sys.argv[1]); apk=p/'Quest3D-public-review-unsigned.apk'
log=(p/(sys.argv[2]+'-private.log')).read_text(errors='replace')
if re.search(r'(?m)^SCRIPT ERROR:|Parse Error:|Failed to compile script|Failed to load script|Could not resolve script',log):
    raise RuntimeError('Godot reported script compilation/loading errors; candidate is not ready')
with zipfile.ZipFile(apk) as z:
    names=z.namelist()
    assert len(names)==len(set(names)), 'Duplicate APK member'
    assert not any(n.startswith('META-INF/') and n.endswith(('.RSA','.DSA','.EC')) for n in names), 'Unexpected APK signature'
    required=['lib/arm64-v8a/libgodot_android.so',
              'lib/arm64-v8a/libnightfall-stream.android.template_release.arm64.so',
              'lib/arm64-v8a/libnightfall-xr.android.template_release.arm64.so',
              'lib/arm64-v8a/libgodotopenxrvendors.so',
              'lib/arm64-v8a/libopenxr_loader.so','classes.dex']
    for n in required: assert n in names, 'Required native/DEX missing: '+n
    script_count=sum(n.endswith('.gdc') for n in names)
    assert 'assets/main.gd.remap' in names and script_count>0, 'Fresh compiled scripts missing'
    assert not any(n.lower().endswith(('.onnx','.tflite','.pth','.pt','.jks','.keystore','.pem','.key')) for n in names), 'Excluded model/key payload'
    report={'schema':1,'compiled_script_count':script_count,'required_native_dex_present':True,
            'script_compile_errors':False,'model_or_private_key_assets':False,'signed':False,
            'public_release_ready':False}
with (p/(sys.argv[2]+'-validation.json')).open('x') as f: json.dump(report,f,indent=2); f.write('\n')
PY
PHASE=complete
