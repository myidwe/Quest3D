"""Apply an exact, UI-only brand overlay to a new preserved Quest source copy.

Android package ID, settings, state paths, protocol and native source are not
changed. The separate Android exporter sets the public app label and version.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from prepare_quest_source import checked, sha, verify_manifest
from prepare_quest_build_baseline import safe_new_output

ALLOWED = {"src/welcome_screen.gd", "src/precision_ui.gd"}


def prepare(source: Path, overlay: Path, output: Path) -> dict:
    source, overlay = source.resolve(strict=True), overlay.resolve(strict=True)
    original = verify_manifest(source)
    declaration = json.loads((overlay / "manifest.json").read_text("utf-8"))
    if declaration.get("base_source_manifest_sha256") != sha(source / "source-manifest.json"):
        raise ValueError("Brand overlay baseline differs from the exact preserved source")
    files = declaration.get("files", {})
    if set(files) != ALLOWED or declaration.get("display_name") != "Sterevi":
        raise ValueError("Only the reviewed Sterevi UI labels can be changed")
    inventory = {p.relative_to(overlay / "files").as_posix() for p in (overlay / "files").rglob("*") if p.is_file()}
    if inventory != ALLOWED:
        raise ValueError("Brand overlay inventory differs")
    for name, record in files.items():
        before = checked(source / "project" / name, source)
        after = checked(overlay / "files" / name, overlay)
        if sha(before) != record.get("before_sha256") or sha(after) != record.get("sha256"):
            raise ValueError("Brand overlay source hashes differ")
        if before.read_text("utf-8").replace('"Quest3D"', '"Sterevi"') != after.read_text("utf-8"):
            raise ValueError("Brand overlay must change only visible product labels")
    output = safe_new_output(output, source)
    if output.is_relative_to(overlay) or overlay.is_relative_to(output):
        raise ValueError("Brand source output must be separate from its overlay")
    shutil.copytree(source, output)
    for name in files:
        shutil.copyfile(overlay / "files" / name, output / "project" / name)
    shutil.copytree(overlay, output / "brand-overlay")
    provenance = {"schema": 1, "kind": "ui-only-product-rebrand", "display_name": "Sterevi",
                  "base_source_manifest_sha256": sha(source / "source-manifest.json"),
                  "overlay_manifest_sha256": sha(overlay / "manifest.json"), "files": files,
                  "package_preserved": "app.questto3d.client", "godot_user_data_name_preserved": "Nightfall",
                  "native_changed": False, "protocol_changed": False, "settings_changed": False,
                  "new_apk_export_required": True, "installed": False, "hardware_verified": False}
    (output / "BRAND_PROVENANCE.json").write_text(json.dumps(provenance, indent=2) + "\n", "utf-8")
    manifest = dict(original, branding=provenance, source_complete=False, clean_build_verified=False,
                    dependency_notices_verified=False, public_release_ready=False)
    manifest["files"] = {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob("*"))
                         if p.is_file() and p.name != "source-manifest.json"}
    (output / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    verify_manifest(output)
    return {"prepared": True, "files": len(manifest["files"]), "brand_overlay_files": len(files),
            "display_name": "Sterevi", "source_manifest_sha256": sha(output / "source-manifest.json"),
            "new_apk_export_required": True, "native_changed": False, "published": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "overlay", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.overlay, args.output), indent=2))
