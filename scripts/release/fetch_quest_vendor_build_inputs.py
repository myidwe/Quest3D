"""Fetch exact vendor build inputs from one official, immutable Git tree.

Sample media made the complete upstream archive exceed the initial 64 MiB
cap. Fetch build inputs only; pin every fetched file by its Git blob identity
and keep unresolved submodule source as an explicit remaining item.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

COMMIT = "6a04c8632140f7dc14670e5564fd473464047a15"
BASE = "https://raw.githubusercontent.com/GodotVR/godot_openxr_vendors/" + COMMIT + "/"
FILE_LIMIT = 12 * 1024 * 1024
TOTAL_LIMIT = 32 * 1024 * 1024
TOP = {"LICENSE", "README.md", ".gitmodules", "SConstruct", "build.gradle", "config.gradle",
       "CONTRIBUTORS.md", "CHANGES.md", "build_raw_headers.py", "settings.gradle", "gradle.properties", "gradlew", "gradlew.bat"}


def fetch(tree_file: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("Preserve prior vendor source inputs")
    tree = json.loads(tree_file.read_text("utf-8"))
    if tree.get("truncated") is not False or tree.get("sha") != COMMIT:
        raise ValueError("Exact, complete official source tree required")
    selected = [e for e in tree["tree"] if e["type"] == "blob" and
                (e["path"] in TOP or e["path"].startswith(("plugin/", "thirdparty/", "doc_classes/", "gradle/", "scripts/", "hooks/", ".github/", "demo/addons/")))]
    if not selected or sum(e.get("size", 0) for e in selected) > TOTAL_LIMIT:
        raise ValueError("Vendor build input download exceeds bounded total")
    seen = set()
    for entry in selected:
        path = PurePosixPath(entry["path"])
        if path.is_absolute() or any(p in {"", ".", ".."} for p in path.parts) or "\\" in entry["path"] or ":" in entry["path"]:
            raise ValueError("Unsafe official source path")
        if entry["path"].casefold() in seen or entry["mode"] not in {"100644", "100755"} or entry.get("size", 0) > FILE_LIMIT:
            raise ValueError("Unsupported official source input")
        seen.add(entry["path"].casefold())
    output.mkdir(parents=True)
    def one(entry):
        with urlopen(Request(BASE + entry["path"], headers={"User-Agent": "Quest3D-source-review/1"}), timeout=60) as response:
            if not response.url.startswith("https://raw.githubusercontent.com/"):
                raise ValueError("Official file response changed host")
            data = response.read(FILE_LIMIT + 1)
        if len(data) != entry["size"] or len(data) > FILE_LIMIT:
            raise ValueError("Vendor source size differs from exact Git tree")
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if blob != entry["sha"]:
            raise ValueError("Vendor source Git blob differs")
        target = output / "source" / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return entry["path"], {"git_blob": blob, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    with ThreadPoolExecutor(max_workers=6) as pool:
        files = dict(pool.map(one, selected))
    result = {"schema": 1, "source_commit": COMMIT, "official_repository": "https://github.com/GodotVR/godot_openxr_vendors",
              "files": files, "file_count": len(files), "bytes": sum(f["bytes"] for f in files.values()),
              "exact_git_blobs_verified": True,
              "unresolved_submodules": [e for e in tree["tree"] if e["type"] == "commit"],
              "vendor_built_from_source": False, "dependency_notices_verified": False,
              "public_release_ready": False}
    with (output / "vendor-build-inputs.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2); stream.write("\n")
    return {"file_count": result["file_count"], "bytes": result["bytes"], "exact_git_blobs_verified": True,
            "unresolved_submodules": len(result["unresolved_submodules"]), "public_release_ready": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(fetch(args.tree, args.output), indent=2))


if __name__ == "__main__":
    main()
