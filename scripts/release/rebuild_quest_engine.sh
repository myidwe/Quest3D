#!/usr/bin/env bash
# Rebuild only the patched Android engine from the isolated prepared source.
set -euo pipefail
[[ $# -ge 2 && $# -le 3 ]] || { echo 'Usage: rebuild_quest_engine.sh PREPARED_BASELINE PINNED_CACHE [JOBS]' >&2; exit 2; }
BASELINE="$(cd "$1" && pwd -P)"
TOOLS="$(cd "$2" && pwd -P)"
JOBS="${3:-6}"
[[ "$JOBS" =~ ^[1-9][0-9]?$ ]] || exit 2
test -f "$BASELINE/quest-build-preparation.json"
test ! -e "$BASELINE/engine-build.started"
test ! -e "$BASELINE/engine-build-summary.json"
[[ "$BASELINE" == /mnt/a/* ]] || { echo 'New outputs must use A drive' >&2; exit 2; }
touch "$BASELINE/engine-build.started"
mkdir -p "$BASELINE/tmp" "$BASELINE/runtime"
export TMPDIR="$BASELINE/tmp"
export PATH="$TOOLS/venv/bin:/usr/bin:$PATH"
export ANDROID_HOME="$TOOLS/android-sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export ANDROID_NDK_ROOT="$ANDROID_HOME/ndk/29.0.14206865"
export JAVA_HOME="$TOOLS/linux/jdk-17.0.20.1+1"
export XDG_DATA_HOME="$BASELINE/cache/xdg-data"
export XDG_CONFIG_HOME="$BASELINE/cache/xdg-config"
export XDG_CACHE_HOME="$BASELINE/cache/xdg-cache"
START="$(date +%s)"
PHASE=android-release-engine
trap 'status=$?; python3 - "$BASELINE" "$PHASE" "$status" "$START" <<"PY"
import hashlib,json,pathlib,sys,time
p=pathlib.Path(sys.argv[1]); outputs={}
for f in (p/"runtime").glob("*.so"):
    outputs[f.name]={"sha256":hashlib.sha256(f.read_bytes()).hexdigest(),"bytes":f.stat().st_size}
result={"schema":1,"phase":sys.argv[2],"exit_code":int(sys.argv[3]),"elapsed_seconds":int(time.time())-int(sys.argv[4]),"clean_android_engine_build_verified":int(sys.argv[3])==0,"clean_editor_build_verified":False,"public_release_ready":False,"outputs":outputs,"ndk":"29.0.14206865","variant":"template_release"}
with (p/"engine-build-summary.json").open("x") as f: json.dump(result,f,indent=2); f.write("\n")
PY
exit "$status"' EXIT
exec > >(tee "$BASELINE/engine-build-private.log") 2>&1
scons -C "$BASELINE/native-source/godot" platform=android target=template_release arch=arm64 -j"$JOBS"
RUNTIME="$BASELINE/native-source/godot/platform/android/java/lib/libs/release/arm64-v8a/libgodot_android.so"
test -f "$RUNTIME"
cp "$RUNTIME" "$BASELINE/runtime/libgodot_android.so"
cp "$BASELINE/native-source/godot/platform/android/java/lib/libs/release/arm64-v8a/libc++_shared.so" "$BASELINE/runtime/libc++_shared.so"
PHASE=complete
