"""Fetch one immutable official OpenXR vendor source archive, with a size limit."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen, Request

COMMIT = "6a04c8632140f7dc14670e5564fd473464047a15"
URL = "https://codeload.github.com/GodotVR/godot_openxr_vendors/tar.gz/" + COMMIT
LIMIT = 64 * 1024 * 1024


def fetch(output: Path) -> dict:
    if output.exists():
        raise FileExistsError("Use a new official vendor source directory")
    output.mkdir(parents=True)
    path = output / "godot_openxr_vendors-source.tar.gz"
    digest = hashlib.sha256()
    total = 0
    with urlopen(Request(URL, headers={"User-Agent": "Quest3D-source-review/1"}), timeout=60) as response:
        if not response.url.startswith("https://codeload.github.com/"):
            raise ValueError("Official source response redirected to another host")
        with path.open("xb") as target:
            while True:
                data = response.read(1024 * 1024)
                if not data:
                    break
                total += len(data)
                if total > LIMIT:
                    raise ValueError("Official source archive exceeds approved bounded download")
                digest.update(data)
                target.write(data)
    root = "godot_openxr_vendors-" + COMMIT + "/"
    selected = {}
    with tarfile.open(path) as archive:
        names = archive.getnames()
        if len(names) != len(set(names)) or len(names) > 50000:
            raise ValueError("Ambiguous official source archive")
        for member in archive.getmembers():
            if not member.name.startswith(root):
                raise ValueError("Unexpected vendor source root")
            relative = member.name[len(root):]
            if not member.isfile() or member.size > 2 * 1024 * 1024:
                continue
            if (relative in {"LICENSE", "README.md", ".gitmodules", "SConstruct", "android/build.gradle", "android/gradle.properties"}
                    or relative.endswith(("/build.gradle", "/CMakeLists.txt"))):
                data = archive.extractfile(member).read()
                target = output / "review-inputs" / relative
                if not target.resolve().is_relative_to((output / "review-inputs").resolve()):
                    raise ValueError("Unsafe official source entry")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                selected[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    result = {"schema": 1, "official_source_url": URL, "source_commit": COMMIT,
              "source_archive_sha256": digest.hexdigest(), "source_archive_bytes": total,
              "source_archive_members": len(names), "selected_review_inputs": selected,
              "vendor_built_from_source": False, "dependency_notices_verified": False,
              "public_release_ready": False}
    with (output / "vendor-source-provenance.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2); stream.write("\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(fetch(args.output), indent=2))


if __name__ == "__main__":
    main()
