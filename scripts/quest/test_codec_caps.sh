#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
TEST_OUTPUT="$QUEST_ARTIFACTS/codec-caps-tests"
JAVA_SOURCE="$QUEST_PROJECT/third_party/nightfall/android/src/main/java/com/godot/game"
JAVA_TEST="$QUEST_PROJECT/third_party/nightfall/test/CodecCapabilityDiagnosticsTest.java"
mkdir -p "$TEST_OUTPUT/classes"
"$JAVA_HOME/bin/javac" -d "$TEST_OUTPUT/classes" \
  "$JAVA_SOURCE/CodecCapabilityDiagnostics.java" "$JAVA_TEST"
"$JAVA_HOME/bin/java" -cp "$TEST_OUTPUT/classes" \
  com.godot.game.CodecCapabilityDiagnosticsTest | tee "$TEST_OUTPUT/result.log"
