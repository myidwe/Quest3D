#!/usr/bin/env bash
# Exact vendor commit using public Khronos headers only, no device operations.
set -euo pipefail
[[ $# -eq 2 ]] || { echo 'Usage: rebuild_quest_public_vendor.sh PREPARED_VENDOR PINNED_CACHE' >&2; exit 2; }
BASE="$(cd "$1" && pwd -P)"
TOOLS="$(cd "$2" && pwd -P)"
[[ "$BASE" == /mnt/a/* ]] || exit 2
test ! -e "$BASE/vendor-build-summary.json"
test ! -e "$BASE/vendor-build.started"
python3 - "$BASE" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);m=json.loads((p/'vendor-source-inputs.json').read_text())
assert m['vendor_commit']=='6a04c8632140f7dc14670e5564fd473464047a15'
assert m['meta_preview_headers_selected'] is False
for n,h in m['source_files'].items():
    assert hashlib.sha256((p/'source'/n).read_bytes()).hexdigest()==h,'Vendor source changed: '+n
PY
touch "$BASE/vendor-build.started"
mkdir -p "$BASE/tmp" "$BASE/cache"
export TMPDIR="$BASE/tmp"
export SCONS_CACHE="$BASE/cache"
export ANDROID_HOME="$TOOLS/android-sdk"
export ANDROID_NDK_ROOT="$ANDROID_HOME/ndk/29.0.14206865"
export ANDROID_NDK_HOME="$ANDROID_NDK_ROOT"
export PATH="$TOOLS/venv/bin:$PATH"
START="$(date +%s)"
trap 'status=$?; python3 - "$BASE" "$status" "$START" <<"PY"
import hashlib,json,pathlib,sys,time
p=pathlib.Path(sys.argv[1]);f=p/"source/demo/addons/godotopenxrvendors/.bin/android/template_release/arm64/libgodotopenxrvendors.so"
r={"schema":1,"exit_code":int(sys.argv[2]),"elapsed_seconds":int(time.time())-int(sys.argv[3]),"vendor_built_from_source":int(sys.argv[2])==0,"meta_preview_headers_selected":False,"installed":False,"published":False}
if f.is_file():r["native"]={"path":f.relative_to(p).as_posix(),"sha256":hashlib.sha256(f.read_bytes()).hexdigest(),"bytes":f.stat().st_size}
(p/"vendor-build-summary.json").write_text(json.dumps(r,indent=2)+"\n")
PY
exit "$status"' EXIT
exec > >(tee "$BASE/vendor-build-private.log") 2>&1
cd "$BASE/source"
scons platform=android target=template_release arch=arm64 ndk_version=29.0.14206865 custom_api_file=thirdparty/godot_cpp_gdextension_api/extension_api.json build_profile=thirdparty/godot_cpp_build_profile/build_profile.json -j6
