"""Bind the extracted Android source template to the pinned official package."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from prepare_quest_build_baseline import source_tool

PACKAGE_SHA = "9714459dc071907c0f3d5f17d608faf69e7cda21331fc5d39c4503ffa4e99eec"
PACKAGE_BYTES = 1279207690


def verify(package: Path, template: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("Preserve prior template verification")
    if package.stat().st_size != PACKAGE_BYTES or source_tool.sha(package) != PACKAGE_SHA:
        raise ValueError("Official Godot template package differs from download lock")
    with zipfile.ZipFile(package) as archive:
        members = [m for m in archive.infolist() if m.filename == "templates/android_source.zip"]
        if len(members) != 1 or members[0].file_size > 300 * 1024 * 1024:
            raise ValueError("Android template package entry missing or ambiguous")
        digest = hashlib.sha256()
        with archive.open(members[0]) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    actual = source_tool.sha(template)
    if actual != digest.hexdigest() or template.stat().st_size != members[0].file_size:
        raise ValueError("Extracted Android source template differs from official package")
    result = {"schema": 1, "verified": True,
              "official_package": "Godot_v4.7-stable_export_templates.tpz",
              "package_sha256": PACKAGE_SHA, "package_bytes": PACKAGE_BYTES,
              "package_url": "https://github.com/godotengine/godot-builds/releases/download/4.7-stable/Godot_v4.7-stable_export_templates.tpz",
              "entry": "templates/android_source.zip", "android_template_sha256": actual,
              "android_template_bytes": template.stat().st_size, "public_release_ready": False}
    source_tool.json_write(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("package", "template", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(verify(args.package, args.template, args.output), indent=2))


if __name__ == "__main__":
    main()
