#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
stage="${1:-Status}"
mkdir -p "$QUEST_ARTIFACTS"
case "$stage" in
  Prepare) bash "$QUEST_SCRIPTS/prepare.sh" ;;
  Native) bash "$QUEST_SCRIPTS/build_native.sh" ;;
  Stream) bash "$QUEST_SCRIPTS/build_stream.sh" ;;
  Xr) bash "$QUEST_SCRIPTS/build_xr.sh" ;;
  Scripts) bash "$QUEST_SCRIPTS/test_scripts.sh" ;;
  Tls) bash "$QUEST_SCRIPTS/test_tls.sh" ;;
  Identity) bash "$QUEST_SCRIPTS/test_frame_identity.sh" ;;
  Apk) bash "$QUEST_SCRIPTS/build_apk.sh" ;;
  Verify) bash "$QUEST_SCRIPTS/verify_apk.sh" ;;
  All)
    for next in Prepare Native Stream Xr Scripts Tls Identity Apk Verify; do
      bash "$QUEST_SCRIPTS/run.sh" "$next"
    done ;;
  *) echo "Unknown stage: $stage" >&2; exit 2 ;;
esac
