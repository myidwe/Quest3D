# Quest display settings source overlay

Canonical delta for the approved 2026-09-29 screen settings. `files/` contains the
reviewed source, `manifest.json` pins original and replacement SHA-256 values,
and `display-settings.patch` is the readable diff against the installed 9/23
source. Keep this overlay separate from unrelated `third_party/nightfall` work.

Prepare with `scripts/prepare-quest-display-build.py --output <new artifacts/quest directory>`.
Existing directories are never replaced. The local pinned baseline is required.
Build the Android native XR library, export via `quest-display-export.sh`,
assemble/sign the APK, and run `quest-display-verify-runtime.sh` before installation.

Detailed commands and current verification: [display settings record](../../docs/DISPLAY_SETTINGS_2026-09-29.md).
The APK contains only seven changed compiled scripts, one pointer shader, one
native XR library and the updated sparse index. Streaming/discovery, engine,
other native libraries and existing assets come from the exact verified APK.

The final repeat-action cleanup is covered by a regression that fails on the
initial candidate and passes on the final compiled product. Test fixtures are
added to the CPU verification pack, not substituted for product scripts.
