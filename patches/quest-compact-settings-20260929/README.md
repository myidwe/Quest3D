# Compact Quest settings

Reviewed layout delta on top of the 2026-09-29 display-settings build. The
working Nightfall checkout is not modified. Two product scripts change: menu
dimensions and PrecisionUI layout/copy. Five files cover UI tests and rendering.

Use `scripts/prepare-quest-compact-build.py --output <new artifacts/quest path>`.
This replays the preceding display-settings overlay, validates its pinned
source/APK/native library, then applies this manifest. Existing output paths
are never replaced. The native XR library is reused byte-for-byte.

Export, assemble with `--extra-test test_compact_settings`, sign, verify signed
payloads, then run `scripts/quest-compact-verify-runtime.sh` before installation.
The fixture tests the signed product GDC rather than replacement product source.

[Design and verification record](../../docs/QUEST_COMPACT_UI_2026-09-29.md)
