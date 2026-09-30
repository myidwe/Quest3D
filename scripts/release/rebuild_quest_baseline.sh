#!/usr/bin/env bash
# Isolated native source build only. No signing, installation, or publication.
set -euo pipefail
if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo 'Usage: rebuild_quest_baseline.sh NEW_PREPARED_BASELINE EXISTING_PINNED_CACHE [JOBS]' >&2
  exit 2
fi
BASELINE="$(cd "$1" && pwd -P)"
TOOLS="$(cd "$2" && pwd -P)"
JOBS="${3:-6}"
[[ "$JOBS" =~ ^[1-9][0-9]?$ ]] || { echo 'Invalid jobs count' >&2; exit 2; }
test -f "$BASELINE/quest-build-preparation.json"
test ! -e "$BASELINE/native-build-summary.json"
test ! -e "$BASELINE/native-build.started"
python3 - "$BASELINE" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
m = json.loads((p/'quest-build-preparation.json').read_text())
assert m['kind'] == 'isolated-quest-source-build-baseline'
assert m['public_preset_change']['signed'] is False
assert p.as_posix().startswith('/mnt/a/'), 'New build output must use the selected A drive'
for component, commit in [('engine_commit','5b4e0cb0fd279832bbdd69fed5354d4e5ad26f88'),('godot_cpp_commit','05057de73de4b99f114d36c40d84ca46926c0e25')]:
    assert m[component] == commit
PY
touch "$BASELINE/native-build.started"
mkdir -p "$BASELINE/tmp" "$BASELINE/cache/xdg-data" "$BASELINE/cache/xdg-config" "$BASELINE/cache/xdg-cache"
export TMPDIR="$BASELINE/tmp"
export XDG_DATA_HOME="$BASELINE/cache/xdg-data"
export XDG_CONFIG_HOME="$BASELINE/cache/xdg-config"
export XDG_CACHE_HOME="$BASELINE/cache/xdg-cache"
export PATH="$TOOLS/venv/bin:$PATH"
export ANDROID_HOME="$TOOLS/android-sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export ANDROID_NDK_ROOT="$ANDROID_HOME/ndk/29.0.14206865"
export ANDROID_NDK_HOME="$ANDROID_NDK_ROOT"
export JAVA_HOME="$TOOLS/linux/jdk-17.0.20.1+1"
export NIGHTFALL_GODOT_SOURCE="$BASELINE/native-source/godot"
export NIGHTFALL_GODOT_CPP="$BASELINE/native-source/godot-cpp"
export NIGHTFALL_BUILD_JOBS="$JOBS"
export NIGHTFALL_NATIVE_XR_CACHE="$BASELINE/native-source"
export NIGHTFALL_INSTALL=0
EDITOR="$TOOLS/native-xr/godot/bin/godot.linuxbsd.editor.x86_64"
INSTALLED="$TOOLS/source/addons/nightfall-stream/build/android/vcpkg_installed"
test -x "$EDITOR"
test -f "$ANDROID_NDK_ROOT/build/cmake/android.toolchain.cmake"
test -f "$INSTALLED/arm64-android/lib/libavcodec.a"
python3 - "$BASELINE" "$TOOLS" "$INSTALLED" "$EDITOR" <<'PY'
import hashlib, json, pathlib, sys
p,tools,installed,editor=map(pathlib.Path,sys.argv[1:])
m=json.loads((p/'quest-build-preparation.json').read_text())
def sha(f):
    h=hashlib.sha256()
    with f.open('rb') as s:
        for b in iter(lambda:s.read(1024*1024),b''): h.update(b)
    return h.hexdigest()
for name,expected in m['copied_project_files'].items():
    if name == 'project/export_presets.cfg': expected=m['public_preset_change']['after_sha256']
    assert sha(p/name)==expected, 'Prepared source changed: '+name
inputs={}
for name,record in m['expected_static_link_inputs'].items():
    f=installed/'arm64-android/lib'/name
    actual=sha(f)
    assert actual==record['sha256'], 'Cached dependency differs: '+name
    inputs[name]={'sha256':actual,'bytes':f.stat().st_size,'rebuilt_in_this_run':False}
assert len(inputs)==13, 'Exact recorded static inputs required'
props=(tools/'android-sdk/ndk/29.0.14206865/source.properties').read_text()
assert '29.0.14206865' in props
result={'schema':1,'editor':{'sha256':sha(editor),'bytes':editor.stat().st_size,'rebuilt_in_this_run':False},'static_link_inputs':inputs,'ndk_source_properties_sha256':sha(tools/'android-sdk/ndk/29.0.14206865/source.properties')}
with (p/'native-build-inputs.json').open('x') as s: json.dump(result,s,indent=2); s.write('\n')
PY
START="$(date +%s)"
PHASE=preflight
trap 'status=$?; python3 - "$BASELINE" "$PHASE" "$status" "$START" <<"PY"
import hashlib, json, pathlib, sys, time
p=pathlib.Path(sys.argv[1]); outputs={}
for f in p.glob("project/**/bin/android/*.so"):
    outputs[f.relative_to(p).as_posix()]={"sha256":hashlib.sha256(f.read_bytes()).hexdigest(),"bytes":f.stat().st_size}
result={"schema":1,"phase":sys.argv[2],"exit_code":int(sys.argv[3]),"elapsed_seconds":int(time.time())-int(sys.argv[4]),"native_build_verified":int(sys.argv[3])==0,"clean_editor_build_verified":False,"clean_engine_build_verified":False,"dependency_notices_verified":False,"full_apk_build_verified":False,"public_release_ready":False,"outputs":outputs,"existing_editor_used_only_for_api_dump":True,"static_dependencies_reused_from_pinned_cache":True}
with (p/"native-build-summary.json").open("x") as f: json.dump(result,f,indent=2); f.write("\n")
PY
exit "$status"' EXIT
exec > >(tee "$BASELINE/native-build-private.log") 2>&1
cmake --version
ninja --version
scons --version
"$JAVA_HOME/bin/java" -version
"$ANDROID_NDK_ROOT/toolchains/llvm/prebuilt/linux-x86_64/bin/clang" --version
PHASE=api-dump
(cd "$NIGHTFALL_GODOT_CPP" && "$EDITOR" --headless --dump-extension-api --dump-gdextension-interface-json --dump-gdextension-interface)
cp "$NIGHTFALL_GODOT_CPP/gdextension_interface.json" "$NIGHTFALL_GODOT_CPP/gdextension/gdextension_interface.json"
PHASE=godot-cpp-release
scons -C "$NIGHTFALL_GODOT_CPP" platform=android target=template_release arch=arm64 \
  android_api_level=24 ndk_version=29.0.14206865 \
  custom_api_file="$NIGHTFALL_GODOT_CPP/extension_api.json" -j"$JOBS"
PHASE=xr-release
bash "$BASELINE/project/extensions/nightfall-xr/build_android.sh" release
PHASE=stream-release-configure
export VCPKG_ROOT="$TOOLS/vcpkg"
cmake -S "$BASELINE/project/addons/nightfall-stream" -B "$BASELINE/build/stream-arm64-release" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_SYSTEM_NAME=Android \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLS/vcpkg/scripts/buildsystems/vcpkg.cmake" \
  -DVCPKG_CHAINLOAD_TOOLCHAIN_FILE="$ANDROID_NDK_ROOT/build/cmake/android.toolchain.cmake" \
  -DVCPKG_MANIFEST_MODE=OFF -DVCPKG_INSTALLED_DIR="$INSTALLED" -DVCPKG_TARGET_TRIPLET=arm64-android \
  -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28 -DNIGHTFALL_PLATFORM=android \
  -DGODOTCPP_SUFFIX=.android.template_release.arm64 -DBUILD_TESTING=OFF
PHASE=stream-release-build
cmake --build "$BASELINE/build/stream-arm64-release" -j "$JOBS"
PHASE=complete
