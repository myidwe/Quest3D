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

Historical design and verification notes are not included in this public source
export. For the current screen and menu controls, see the
[user guide](../../docs/DESKTOP_USER_GUIDE.md#화질-조절).
