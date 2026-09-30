"""Stage a built host in a fresh directory; never installs, starts or promotes it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked(path: Path, *, missing: bool = False) -> Path:
    """Check lexical ancestors, retaining aliases so Windows junctions are refused."""
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        try:
            info = node.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Redirected candidate input or output")
    return path


def read(path: Path) -> bytes:
    path = checked(path)
    if not path.is_file():
        raise ValueError("Candidate input must be a regular file")
    return path.read_bytes()


def files(root: Path, exclude: str | None = None):
    root = checked(root)
    if not root.is_dir():
        raise ValueError("Required asset directory missing")
    for parent, dirs, names in os.walk(root, followlinks=False):
        checked(Path(parent))
        if Path(parent) == root and exclude:
            dirs[:] = [name for name in dirs if name != exclude]
        for name in dirs:
            checked(Path(parent) / name)
        for name in sorted(names):
            path = Path(parent) / name
            yield path.relative_to(root).as_posix(), path


def prepare(candidate: Path, zlib: Path, output: Path) -> dict:
    candidate, zlib = checked(candidate), checked(zlib)
    output = checked(output, missing=True)
    if output.exists() or output.is_relative_to(candidate) or candidate.is_relative_to(output):
        raise FileExistsError("Use a separate new runtime candidate directory")
    provenance_path = candidate / "candidate-provenance.json"
    summary_path = candidate / "candidate-build-summary.json"
    provenance = json.loads(read(provenance_path))
    summary = json.loads(read(summary_path))
    if (provenance.get("kind") != "historical-api-compatibility-candidate" or
            summary.get("native_build_success") is not True or
            summary.get("tests_passed") is not True or summary.get("exit_code") != 0):
        raise ValueError("Successful reviewed native candidate required")
    build = candidate / "cmake-build-compat-candidate"
    source = candidate / "source"
    payload: dict[str, tuple[bytes, dict]] = {}

    def put(name: str, path: Path, origin: str):
        if any(existing.casefold() == name.casefold() for existing in payload):
            raise ValueError("Duplicate asset destination")
        data = read(path)
        payload[name] = data, {"origin": origin, "sha256": sha(data), "bytes": len(data)}

    put("sunshine.exe", build / "sunshine.exe", "native-build")
    if payload["sunshine.exe"][1]["sha256"] != summary.get("host_exe_sha256"):
        raise ValueError("Native executable differs from the successful build report")
    if zlib.name.casefold() != "zlib1.dll":
        raise ValueError("Explicit zlib1.dll input required")
    put("zlib1.dll", zlib, "explicit-toolchain-input-hash-recorded")
    put("LICENSE.txt", source / "LICENSE", "host-source")
    for relative, path in files(source / "src_assets/common/assets", exclude="web"):
        put("assets/" + relative, path, "common-source-asset")
    for relative, path in files(source / "src_assets/windows/assets"):
        put("assets/" + relative, path, "windows-source-asset")
    for relative, path in files(build / "assets/web"):
        put("assets/web/" + relative, path, "web-build")
    for required in ("assets/apps.json", "assets/web/index.html", "assets/web/pin.html",
                     "assets/web/password.html", "assets/web/welcome.html"):
        if required not in payload:
            raise ValueError("Web UI or required static asset has not been built")
    if not any(name.startswith("assets/shaders/") for name in payload):
        raise ValueError("Windows capture shader source assets missing")
    # Collect every input before creating the output. Never follow the build's
    # CMake-created shaders junction; use its actual source directory above.
    report = {"schema": 1, "kind": "uninstalled-host-runtime-candidate",
              "candidate_version": provenance.get("candidate_version"),
              "host_exe_sha256": summary["host_exe_sha256"],
              "source_provenance_sha256": sha(read(provenance_path)),
              "native_build_report_sha256": sha(read(summary_path)),
              "files": {name: item[1] for name, item in sorted(payload.items())},
              "file_count": len(payload), "web_ui_packaged": True,
              "installed": False, "server_started": False,
              "historical_binary_source_correspondence_verified": False,
              "release_source_gate": False, "hardware_validation": False,
              "notes": "New compile candidate. No old runtime/config/credentials copied; dependency notices and integration validation remain."}
    output.mkdir(parents=True, exist_ok=False)
    for name, (data, _) in payload.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        checked(destination, missing=True)
        with destination.open("xb") as stream:
            stream.write(data)
    with (output / "runtime-preparation.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--zlib", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.candidate, args.zlib, args.output)
    print(json.dumps({key: report[key] for key in ("file_count", "host_exe_sha256", "web_ui_packaged", "release_source_gate")}, indent=2))


if __name__ == "__main__":
    main()
