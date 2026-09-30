#!/usr/bin/env bash
# Resolve actual runtime components in an A-only copy of the pinned Gradle cache.
set -euo pipefail
[[ $# -eq 2 ]] || { echo 'Usage: audit_quest_gradle_dependencies.sh PREPARED_EXPORT PINNED_CACHE' >&2; exit 2; }
EXPORT="$(cd "$1" && pwd -P)"
TOOLS="$(cd "$2" && pwd -P)"
test -f "$EXPORT/android-export-preparation.json"
test ! -e "$EXPORT/dependency-audit-standard-release.started"
[[ "$EXPORT" == /mnt/a/* ]] || exit 2
touch "$EXPORT/dependency-audit-standard-release.started"
export JAVA_HOME="$TOOLS/linux/jdk-17.0.20.1+1"
export ANDROID_HOME="$TOOLS/android-sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export GRADLE_USER_HOME="$EXPORT/cache/gradle"
export TMPDIR="$EXPORT/tmp"
mkdir -p "$GRADLE_USER_HOME" "$TMPDIR"
python3 - "$TOOLS/gradle" "$GRADLE_USER_HOME" "$EXPORT" <<'PY'
import pathlib,shutil,sys
origin,target,out=map(pathlib.Path,sys.argv[1:])
for relative in ('wrapper','caches/modules-2'):
    source=origin/relative
    assert source.is_dir(), 'Pinned Gradle cache missing'
    if not (target/relative).exists():
        shutil.copytree(source,target/relative,ignore=shutil.ignore_patterns('*.lock','*.lck'))
init=out/'runtime-dependencies-standard-release.init.gradle'
init.write_text('''import groovy.json.JsonOutput
gradle.projectsEvaluated {
    rootProject.tasks.register("quest3dRuntimeDependencyReport") {
        doLast {
            def cfg = rootProject.configurations.getByName("standardReleaseRuntimeClasspath")
            def rows = cfg.resolvedConfiguration.resolvedArtifacts.collect { a ->
                def id = a.moduleVersion.id
                def bytes = a.file.bytes
                [group:id.group, name:id.name, version:id.version, type:a.type,
                 classifier:a.classifier, filename:a.file.name, bytes:bytes.length,
                 sha256:java.security.MessageDigest.getInstance("SHA-256").digest(bytes).encodeHex().toString()]
            }.sort { a,b -> "${a.group}:${a.name}:${a.version}" <=> "${b.group}:${b.name}:${b.version}" }
            new File(rootProject.projectDir, "quest3d-resolved-runtime-standard-release.json").text = JsonOutput.prettyPrint(JsonOutput.toJson(rows))
        }
    }
}
''')
PY
START="$(date +%s)"
trap 'status=$?; python3 - "$EXPORT" "$status" "$START" <<"PY"
import json,pathlib,sys,time
p=pathlib.Path(sys.argv[1]); resolved=p/"project/android/build/quest3d-resolved-runtime-standard-release.json"
result={"schema":1,"exit_code":int(sys.argv[2]),"elapsed_seconds":int(time.time())-int(sys.argv[3]),"offline":True,"read_only_original_cache":True,"runtime_configuration":"standardReleaseRuntimeClasspath","resolved":resolved.is_file(),"public_release_ready":False,"dependency_notices_verified":False}
if resolved.is_file(): result["runtime_dependencies"]=json.loads(resolved.read_text())
with (p/"dependency-audit-standard-release-summary.json").open("x") as f: json.dump(result,f,indent=2); f.write("\n")
PY
exit "$status"' EXIT
exec > >(tee "$EXPORT/dependency-audit-standard-release-private.log") 2>&1
cd "$EXPORT/project/android/build"
bash gradlew --offline --no-daemon --console=plain -I "$EXPORT/runtime-dependencies-standard-release.init.gradle" \
  -Pexport_package_name=app.questto3d.client -Pexport_enabled_abis=arm64-v8a \
  -Pexport_min_sdk=29 -Pexport_target_sdk=32 \
  quest3dRuntimeDependencyReport
