"""Assemble pinned historical Sunshine source in a new directory, without downloads.

This is a provenance/buildability investigation. Successful compilation alone
does not prove that the historical executable was built from this snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
PIN = "cb72dffa3233c5815cd5ba88f09f049dd679ba75"
HOST_SHA = "77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63"
SNAPSHOT = "artifacts/host/patch-reproduction-f6e4f3c7490a4c9eac9cf2ea37a4342d"
REQUIRED_WINDOWS_MODULES = {
    "third-party/build-deps", "third-party/glad", "third-party/libdisplaydevice",
    "third-party/libvirtualhid", "third-party/lizardbyte-common",
    "third-party/moonlight-common-c", "third-party/nvapi",
    "third-party/Simple-Web-Server", "third-party/ViGEmClient",
}

_spec = importlib.util.spec_from_file_location("host_source_bundle", Path(__file__).with_name("build_bundle.py"))
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)
_spec = importlib.util.spec_from_file_location("host_source_audit", Path(__file__).with_name("export_repository.py"))
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repository), *args], capture_output=True, check=True)
    return result.stdout


def extract_archive(data: bytes, destination: Path) -> None:
    """Extract only regular files/directories; reject escapes and aliases first."""
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        names = set()
        for member in archive.getmembers():
            name = bundle.relative_name(member.name.rstrip("/"))
            if name.casefold() in names or not (member.isfile() or member.isdir()):
                raise ValueError("Unsupported or duplicate source archive member")
            names.add(name.casefold())
        destination.mkdir(parents=True, exist_ok=True)
        archive.extractall(destination, filter="data")


def hashes(root: Path) -> dict[str, str]:
    """Check every directory once before descending into its regular files.

    os.walk never follows symlinks. Explicit reparse checks also reject Windows
    junctions, which must be rejected before walk can visit a child directory.
    """
    root = Path(os.path.abspath(root))
    result = {}
    def regular_info(path: Path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Reparse point in prepared source")
        return info
    if not stat.S_ISDIR(regular_info(root).st_mode):
        raise NotADirectoryError("Prepared source root must be a directory")
    for parent, dirs, files in os.walk(root, followlinks=False):
        regular_info(Path(parent))
        for name in dirs:
            regular_info(Path(parent) / name)
        for name in sorted(files):
            path = Path(parent) / name
            if not stat.S_ISREG(regular_info(path).st_mode):
                raise ValueError("Non-regular source file")
            result[path.relative_to(root).as_posix()] = bundle.digest(path)
    return result


def expected_archive_files(output: Path, snapshot_hashes) -> dict:
    """Derive exact assembled bytes from public Git object archives + overlay."""
    expected = {}
    for archive_path in sorted((output / "archives").glob("*.tar")):
        prefix = "" if archive_path.name == "sunshine-upstream.tar" else archive_path.stem.replace("--", "/") + "/"
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                name = bundle.relative_name(member.name.rstrip("/"))
                if member.isfile():
                    expected[prefix + name] = sha(archive.extractfile(member).read())
    expected.update(snapshot_hashes)
    return expected


def finalize(root: Path, output: Path) -> dict:
    """Finish a prepared tree without recopying or modifying source inputs."""
    if (output / "source-preparation.json").exists():
        raise FileExistsError("The prepared source manifest already exists")
    source = root / "third_party/sunshine"
    snapshot_hashes = hashes(root / SNAPSHOT)
    expected = expected_archive_files(output, snapshot_hashes)
    actual = hashes(output / "source")
    if actual != expected or hashes(output / "historical-overlay") != snapshot_hashes:
        raise ValueError("Assembled source does not match archived authority")
    runtime = root / "artifacts/host/runtime-20260909-233013-03efb525/sunshine.exe"
    if bundle.digest(runtime) != HOST_SHA:
        raise ValueError("Historical runtime changed")
    state = {"schema": 1, "kind": "historical-host-source-investigation", "published": False,
        "host_binary_sha256": HOST_SHA, "upstream_commit": PIN,
        "overlay_files": snapshot_hashes, "source_files": actual,
        "archives": {p.name: {"sha256": bundle.digest(p), "bytes": p.stat().st_size}
                     for p in sorted((output / "archives").glob("*.tar"))},
        "dependencies": {p.name: {"sha256": bundle.digest(p), "bytes": p.stat().st_size}
                         for p in sorted((output / "dependencies").iterdir()) if p.is_file()},
        "assembled_source_matches_archives_and_overlay": True,
        "historical_binary_source_correspondence_verified": False,
        "build_success": False, "bit_for_bit_match_required": False,
        "notes": ["Historical overlay exported 2026-09-09 22:53; prior regression 22:51.",
                  "Prebuilt FFmpeg source correspondence and final runtime behavior remain separate release gates."]}
    (output / "source-preparation.json").write_text(json.dumps(state, indent=2) + "\n", "utf-8")
    return {"prepared": True, "source_files": len(actual),
        "assembled_source_matches_archives_and_overlay": True,
        "historical_binary_source_correspondence_verified": False}


def prepare(root: Path, output: Path) -> dict:
    root = root.resolve()
    if output.exists():
        raise FileExistsError("Use a new host-source preparation directory")
    source = root / "third_party/sunshine"
    snapshot = root / SNAPSHOT
    runtime = root / "artifacts/host/runtime-20260909-233013-03efb525/sunshine.exe"
    bundle.assert_regular(runtime, root)
    if bundle.digest(runtime) != HOST_SHA or git(source, "rev-parse", "HEAD").decode().strip() != PIN:
        raise ValueError("Historical host identity differs")
    snapshot_hashes = hashes(snapshot)
    audit.audit_files(snapshot, snapshot_hashes, audit.default_private_markers())
    # No working-tree patch is read or applied. Every upstream byte comes from
    # the selected Git object; overlay bytes are independently hash checked.
    output.mkdir(parents=True)
    archives = output / "archives"
    archives.mkdir()
    assembled = output / "source"
    upstream = git(source, "archive", "--format=tar", PIN)
    (archives / "sunshine-upstream.tar").write_bytes(upstream)
    extract_archive(upstream, assembled)
    modules = []

    def submodules(repository: Path, pin: str, prefix=""):
        for row in git(repository, "ls-tree", "-r", "-z", pin).split(b"\0"):
            if not row:
                continue
            metadata, path = row.split(b"\t", 1)
            mode, _, commit = metadata.decode().split()
            if mode != "160000":
                continue
            relative = path.decode()
            name = prefix + relative
            child = repository / relative
            record = {"path": name, "commit": commit, "included": (child / ".git").exists()}
            modules.append(record)
            if not record["included"]:
                if name in REQUIRED_WINDOWS_MODULES:
                    raise ValueError("Required Windows submodule unavailable: " + name)
                continue
            data = git(child, "archive", "--format=tar", commit)
            archive_name = name.replace("/", "--") + ".tar"
            (archives / archive_name).write_bytes(data)
            record.update(archive=archive_name, sha256=sha(data))
            extract_archive(data, assembled / name)
            submodules(child, commit, name + "/")

    submodules(source, PIN)
    overlay = output / "historical-overlay"
    overlay.mkdir()
    for name, expected in snapshot_hashes.items():
        original = snapshot / name
        if bundle.digest(original) != expected:
            raise ValueError("Historical source changed during preparation")
        for destination in (overlay / name, assembled / name):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(original.read_bytes())
            if bundle.digest(destination) != expected:
                raise ValueError("Historical source copy mismatch")
    deps = output / "dependencies"
    deps.mkdir()
    existing = source / "cmake-build-quest3d/_deps"
    archives_to_copy = {
        "boost-1.89.0-cmake.tar.xz": existing / "boost-subbuild/boost-populate-prefix/src/boost-1.89.0-cmake.tar.xz",
        "json.tar.xz": existing / "json-subbuild/json-populate-prefix/src/json.tar.xz",
        "Windows-AMD64-ffmpeg.tar.gz": existing / "ffmpeg-v2026.724.203728/Windows-AMD64-ffmpeg.tar.gz",
    }
    dependency_records = {}
    for name, path in archives_to_copy.items():
        bundle.assert_regular(path, root)
        expected = bundle.digest(path)
        shutil.copyfile(path, deps / name)
        if bundle.digest(deps / name) != expected:
            raise ValueError("Dependency copy mismatch")
        dependency_records[name] = {"sha256": expected, "bytes": path.stat().st_size}
    if dependency_records["boost-1.89.0-cmake.tar.xz"]["sha256"] != "67acec02d0d118b5de9eb441f5fb707b3a1cdd884be00ca24b9a73c995511f74":
        raise ValueError("Boost source pin differs")
    if hashlib.md5((deps / "json.tar.xz").read_bytes()).hexdigest() != "c23a33f04786d85c29fda8d16b5f0efd":
        raise ValueError("JSON source pin differs")
    # These are immutable object exports, separate from the mutable CMake cache.
    for version in ("11", "12", "13"):
        repo = existing / f"nv_codec_headers_{version}-src"
        commit = git(repo, "rev-parse", "HEAD").decode().strip()
        data = git(repo, "archive", "--format=tar", commit)
        name = f"nv-codec-headers-{version}.tar"
        (deps / name).write_bytes(data)
        dependency_records[name] = {"commit": commit, "sha256": sha(data), "bytes": len(data)}
        extract_archive(data, deps / f"nv-codec-headers-{version}")
    for name in archives_to_copy:
        # Boost/JSON upstream tarballs have a top-level package directory.
        extract_archive((deps / name).read_bytes(), deps / "unpacked")
    shutil.copyfile(root / "native/host/toolchain.lock.json", output / "toolchain.lock.json")
    shutil.copyfile(Path(__file__).with_name("rebuild_host_source.sh"), output / "rebuild_host_source.sh")
    source_hashes = hashes(assembled)
    if expected_archive_files(output, snapshot_hashes) != source_hashes:
        raise ValueError("Assembled source differs from archives and historical overlay")
    state = {"schema": 1, "kind": "historical-host-source-investigation", "published": False,
        "host_binary_sha256": HOST_SHA, "upstream_commit": PIN,
        "upstream_archive_sha256": sha(upstream), "overlay_files": snapshot_hashes,
        "source_files": source_hashes, "submodules": modules, "dependencies": dependency_records,
        "assembled_source_matches_archives_and_overlay": True,
        "historical_binary_source_correspondence_verified": False,
        "build_success": False, "bit_for_bit_match_required": False,
        "notes": ["Historical overlay exported 2026-09-09 22:53; prior regression 22:51.",
                  "Prebuilt FFmpeg source correspondence and final runtime behavior remain separate release gates."]}
    (output / "source-preparation.json").write_text(json.dumps(state, indent=2) + "\n", "utf-8")
    if hashes(snapshot) != snapshot_hashes or bundle.digest(runtime) != HOST_SHA:
        raise ValueError("Original historical inputs changed")
    return {"prepared": True, "source_files": len(state["source_files"]),
            "submodules_included": sum(x["included"] for x in modules),
            "historical_binary_source_correspondence_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--finalize-only", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(finalize(args.root.resolve(), args.output.resolve()) if args.finalize_only
                     else prepare(args.root, args.output), indent=2))


if __name__ == "__main__":
    main()
