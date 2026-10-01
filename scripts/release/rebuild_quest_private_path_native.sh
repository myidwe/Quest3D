#!/usr/bin/env bash
# Fresh arm64 static dependencies and stream extension; no APK/device operation.
set -euo pipefail
[[ $# -ge 1 && $# -le 2 ]] || { echo 'Usage: rebuild_quest_private_path_native.sh NEW_PREPARED_BASE [JOBS]' >&2; exit 2; }
BASE="$(cd "$1" && pwd -P)"
JOBS="${2:-6}"
[[ "$BASE" == /mnt/a/* && "$JOBS" =~ ^[1-9][0-9]?$ ]] || exit 2
test -f "$BASE/private-path-build-preparation.json"
test ! -e "$BASE/native-build.started"
python3 - "$BASE" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);m=json.loads((p/'private-path-build-preparation.json').read_text())
def sha(f): return hashlib.sha256(f.read_bytes()).hexdigest()
assert m['kind']=='private-path-free-quest-native-build' and m['existing_compiled_dependencies_reused'] is False
for folder,key in [('project/addons/nightfall-stream','source_inputs'),('overlay-ports','overlay_port_inputs')]:
    root=p/folder
    actual={f.relative_to(root).as_posix():sha(f) for f in root.rglob('*') if f.is_file()}
    assert actual==m[key], 'Prepared source or port recipe changed'
assert sha(p/'triplets/arm64-android.cmake')==m['recipe_files']['triplet_sha256']
assert sha(p/'tools/ndk/source.properties')==m['ndk_source_properties_sha256']
assert sha(p/'tools/vcpkg/vcpkg')==m['vcpkg_executable_sha256']
assert not (p/'installed').exists() and not (p/'buildtrees').exists(), 'A fresh dependency build is required'
PY
touch "$BASE/native-build.started"
mkdir -p "$BASE/tmp" "$BASE/output"
export TMPDIR="$BASE/tmp"
export PATH="$BASE/tools/bin:/usr/bin:/bin"
export ANDROID_NDK_HOME="$BASE/tools/ndk"
export ANDROID_NDK_ROOT="$ANDROID_NDK_HOME"
export VCPKG_ROOT="$BASE/tools/vcpkg"
export VCPKG_DISABLE_METRICS=1
export VCPKG_BINARY_SOURCES=clear
export VCPKG_MAX_CONCURRENCY="$JOBS"
export VCPKG_REGISTRIES_CACHE="$BASE/registries"
export VCPKG_DOWNLOADS="$BASE/tools/vcpkg/downloads"
export SOURCE_DATE_EPOCH=1788192000
export XDG_CACHE_HOME="$BASE/cache"
export XDG_CONFIG_HOME="$BASE/config"
export XDG_DATA_HOME="$BASE/data"
unset CFLAGS CXXFLAGS CPPFLAGS LDFLAGS CC CXX CMAKE_TOOLCHAIN_FILE
START="$(date +%s)"
PHASE=dependencies
trap 'status=$?; python3 - "$BASE" "$PHASE" "$status" "$START" <<"PY"
import hashlib,json,pathlib,sys,time
p=pathlib.Path(sys.argv[1]); files={}
for root in (p/"installed/arm64-android/lib",p/"output"):
    for f in sorted(root.glob("*")):
        if f.is_file(): files[f.relative_to(p).as_posix()]={"sha256":hashlib.sha256(f.read_bytes()).hexdigest(),"bytes":f.stat().st_size,"rebuilt_in_this_run":True}
r={"schema":1,"phase":sys.argv[2],"exit_code":int(sys.argv[3]),"elapsed_seconds":int(time.time())-int(sys.argv[4]),"native_build_verified":int(sys.argv[3])==0,"static_dependencies_reused":False,"assertions_disabled":False,"output":files,"installed":False,"published":False}
(p/"native-build-summary.json").write_text(json.dumps(r,indent=2)+"\n")
PY
exit "$status"' EXIT
exec > "$BASE/native-build-private.log" 2>&1
cd "$BASE/project/addons/nightfall-stream"
# The original manifest registry baseline also pins the host build helpers.
# Classic mode would silently choose newer checkout helper versions.
"$VCPKG_ROOT/vcpkg" install \
  --overlay-ports="$BASE/overlay-ports" --overlay-triplets="$BASE/triplets" \
  --x-install-root="$BASE/installed" --x-buildtrees-root="$BASE/buildtrees" --x-packages-root="$BASE/packages" \
  --binarysource=clear --triplet=arm64-android
PHASE=stream-configure
cmake -S "$BASE/project/addons/nightfall-stream" -B "$BASE/build/stream" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_SYSTEM_NAME=Android \
  -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake" \
  -DVCPKG_CHAINLOAD_TOOLCHAIN_FILE="$ANDROID_NDK_HOME/build/cmake/android.toolchain.cmake" \
  -DVCPKG_MANIFEST_MODE=OFF -DVCPKG_INSTALLED_DIR="$BASE/installed" -DVCPKG_TARGET_TRIPLET=arm64-android \
  -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28 -DNIGHTFALL_PLATFORM=android \
  -DCMAKE_C_FLAGS='-ffile-prefix-map=/mnt/a=/quest3d -fdebug-prefix-map=/mnt/a=/quest3d' \
  -DCMAKE_CXX_FLAGS='-ffile-prefix-map=/mnt/a=/quest3d -fdebug-prefix-map=/mnt/a=/quest3d' \
  -DGODOTCPP_SUFFIX=.android.template_release.arm64 -DBUILD_TESTING=OFF -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
PHASE=stream-build
cmake --build "$BASE/build/stream" -j "$JOBS"
cp "$BASE/build/stream/bin/android/libnightfall-stream.android.template_release.arm64.so" "$BASE/output/"
PHASE=complete
