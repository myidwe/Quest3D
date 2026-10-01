"""Prepare a new A-drive native rebuild without reusing compiled dependencies.

The cache supplies only pinned tool executables, downloaded sources, and the
vcpkg checkout. Its installed libraries, buildtrees and binary cache are never
copied. Existing candidates remain unchanged. Run with the pinned WSL Python.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

VCPKG_COMMIT = "f781d9387e4684783e69e136e2e124ff4660bffc"
NDK = "29.0.14206865"
PACKAGES = ("curl", "ffmpeg", "godot-cpp", "moonlight-common-c", "openssl", "opus", "simde", "zlib")

def sha(file: Path) -> str:
    h = hashlib.sha256()
    with file.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def prepare(source: Path, cache: Path, output: Path, repo: Path, *, resume_tool_copy: bool = False, toolchain_ready: bool = False) -> dict:
    source, cache, repo = (p.resolve(strict=True) for p in (source, cache, repo))
    output = output.absolute()
    if not str(output).startswith("/mnt/a/") or (output.exists() and not resume_tool_copy):
        raise ValueError("Use a new A-drive output directory")
    if resume_tool_copy and (not (output / "tools/vcpkg/vcpkg").is_file() or any((output / n).exists() for n in
            ("project", "overlay-ports", "private-path-build-preparation.json", "native-build.started", "installed", "buildtrees"))):
        raise ValueError("Only an unfinished tool copy may be resumed")
    for p in (output, *output.parents):
        if p.is_symlink():
            raise ValueError("Output parents must not be links")
    for p in (source, cache):
        if output.is_relative_to(p) or p.is_relative_to(output):
            raise ValueError("Output must be separate from preserved inputs")
    source_manifest = json.loads((source / "source-manifest.json").read_text())["files"]
    selected_source = [p for p in (source / "project/addons/nightfall-stream").rglob("*") if p.is_file()]
    selected_ports = [p for p in (source / "dependency-supply/native/recipes").rglob("*") if p.is_file()]
    for file in selected_source + selected_ports:
        record = source_manifest.get(file.relative_to(source).as_posix())
        expected = record.get("sha256") if isinstance(record, dict) else record
        if file.is_symlink() or sha(file) != expected:
            raise ValueError("Recorded public source input differs")
    commit = subprocess.check_output(["git", "-C", str(cache / "vcpkg"), "rev-parse", "HEAD"], text=True).strip()
    if commit != VCPKG_COMMIT or subprocess.check_output(["git", "-C", str(cache / "vcpkg"), "status", "--porcelain"], text=True).strip():
        raise ValueError("Pinned clean vcpkg checkout required")
    output.mkdir(parents=True, exist_ok=resume_tool_copy)
    tools = output / "tools"
    tools.mkdir(exist_ok=resume_tool_copy)
    archive = output / "vcpkg-source.tar"
    if not resume_tool_copy:
        with archive.open("xb") as stream:
            subprocess.run(["git", "-C", str(cache / "vcpkg"), "archive", VCPKG_COMMIT], stdout=stream, check=True)
    (tools / "vcpkg").mkdir(exist_ok=resume_tool_copy)
    with tarfile.open(archive) as value:
        for m in value.getmembers():
            p = Path(m.name)
            if p.is_absolute() or ".." in p.parts or m.issym() or m.islnk():
                raise ValueError("Unsafe vcpkg source entry")
        # Native tar/cp avoid slow Python per-file copies on the A-drive mount.
        if not resume_tool_copy:
            subprocess.run(["tar", "-xf", str(archive), "-C", str(tools / "vcpkg")], check=True, capture_output=True)
    shutil.copyfile(cache / "vcpkg/vcpkg", tools / "vcpkg/vcpkg")
    (tools / "vcpkg/vcpkg").chmod(0o755)
    downloads = tools / "vcpkg/downloads"
    downloads.mkdir(exist_ok=resume_tool_copy)
    # Downloads are source archives and host tools, not arm64 build caches.
    for item in (cache / "vcpkg/downloads").iterdir():
        if item.is_file() and not resume_tool_copy:
            shutil.copy2(item, downloads / item.name)
        elif item.name == "tools" and not resume_tool_copy:
            subprocess.run(["cp", "-R", str(item), str(downloads / "tools")], check=True, capture_output=True)
    for name, original in (("ndk", cache / ("android-sdk/ndk/" + NDK)), ("venv", cache / "venv")):
        target = tools / name
        if toolchain_ready:
            if not resume_tool_copy or not target.is_dir():
                raise ValueError("Ready tools require an existing unfinished tool-only candidate")
            required = (["source.properties", "build/cmake/android.toolchain.cmake",
                "toolchains/llvm/prebuilt/linux-x86_64/bin/clang",
                "toolchains/llvm/prebuilt/linux-x86_64/bin/ld.lld",
                "toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-ar",
                "toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip",
                "toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/aarch64-linux-android/libc++_shared.so"]
                if name == "ndk" else ["bin/python", "bin/ninja", "lib/python3.10/site-packages/cmake/data/bin/cmake"])
            if not str(target.resolve()).startswith("/mnt/a/"):
                raise ValueError("Ready tools must resolve on the A drive")
            for item in required:
                if sha(target / item) != sha(original / item):
                    raise ValueError("Ready pinned tool byte mismatch")
            continue
        target.mkdir(exist_ok=resume_tool_copy)
        # DrvFS can reject Unix owner/xattr preservation. Copy bytes, modes and
        # symlinks without requesting Unix ownership or extended attributes.
        subprocess.run(["cp", "-R", "--update", str(original) + "/.", str(target)], check=True, capture_output=True)
    if NDK not in (tools / "ndk/source.properties").read_text():
        raise ValueError("Pinned NDK version differs")
    stream_source = output / "project/addons/nightfall-stream"
    shutil.copytree(source / "project/addons/nightfall-stream", stream_source)
    overlay = output / "overlay-ports"
    overlay.mkdir()
    for package in PACKAGES:
        shutil.copytree(source / "dependency-supply/native/recipes" / package, overlay / package)
    openssl = overlay / "openssl"
    patch = repo / "scripts/release/quest_openssl_private_build_info.patch"
    supplied_patch = openssl / patch.name
    if supplied_patch.exists() and sha(supplied_patch) != sha(patch):
        raise ValueError("Supplied OpenSSL diagnostic metadata patch differs")
    shutil.copyfile(patch, supplied_patch)
    portfile = openssl / "portfile.cmake"
    raw = portfile.read_text()
    if raw.count("    PATCHES\n") != 1:
        raise ValueError("Unexpected OpenSSL port recipe")
    declaration = "        quest_openssl_private_build_info.patch\n"
    if declaration in raw:
        if raw.count(declaration) != 1:
            raise ValueError("OpenSSL metadata patch must be selected once")
    else:
        portfile.write_text(raw.replace("    PATCHES\n", "    PATCHES\n" + declaration))
    triplets = output / "triplets"
    triplets.mkdir()
    (triplets / "arm64-android.cmake").write_text(
        "set(VCPKG_TARGET_ARCHITECTURE arm64)\nset(VCPKG_CRT_LINKAGE dynamic)\n"
        "set(VCPKG_LIBRARY_LINKAGE static)\nset(VCPKG_CMAKE_SYSTEM_NAME Android)\n"
        "set(VCPKG_CMAKE_SYSTEM_VERSION 28)\nset(VCPKG_MAKE_BUILD_TRIPLET \"--host=aarch64-linux-android\")\n"
        "set(VCPKG_CMAKE_CONFIGURE_OPTIONS -DANDROID_ABI=arm64-v8a)\nset(VCPKG_BUILD_TYPE release)\n"
        "set(VCPKG_C_FLAGS \"-ffile-prefix-map=/mnt/a=/quest3d -fdebug-prefix-map=/mnt/a=/quest3d\")\n"
        "set(VCPKG_CXX_FLAGS \"${VCPKG_C_FLAGS}\")\n"
    )
    binpath = tools / "bin"
    binpath.mkdir()
    pkgconfig = cache / "sysroot/usr/bin/pkg-config"
    if not pkgconfig.is_file():
        raise ValueError("Pinned host pkg-config tool required")
    shutil.copyfile(pkgconfig, binpath / "pkg-config")
    (binpath / "pkg-config").chmod(0o755)
    for name, invocation in (("python", '"$HERE/../venv/bin/python"'), ("python3", '"$HERE/../venv/bin/python"'),
                             ("cmake", '"$HERE/../venv/bin/python" -m cmake'), ("ninja", '"$HERE/../venv/bin/ninja"')):
        p = binpath / name
        p.write_text('#!/usr/bin/env bash\nset -euo pipefail\nHERE="$(cd "$(dirname "$0")" && pwd -P)"\nexec ' + invocation + ' "$@"\n')
        p.chmod(0o755)
    provenance = {
        "schema": 1, "kind": "private-path-free-quest-native-build", "vcpkg_commit": commit,
        "vcpkg_source_archive_sha256": sha(archive), "vcpkg_executable_sha256": sha(tools / "vcpkg/vcpkg"),
        "ndk_version": NDK, "ndk_source_properties_sha256": sha(tools / "ndk/source.properties"),
        "source_manifest_sha256": sha(source / "source-manifest.json"),
        "source_inputs": {p.relative_to(stream_source).as_posix(): sha(p) for p in sorted(stream_source.rglob("*")) if p.is_file()},
        "overlay_port_inputs": {p.relative_to(overlay).as_posix(): sha(p) for p in sorted(overlay.rglob("*")) if p.is_file()},
        "recipe_files": {"prepare_helper_sha256": sha(Path(__file__)), "openssl_metadata_patch_sha256": sha(patch),
                         "triplet_sha256": sha(triplets / "arm64-android.cmake")},
        "pkg_config_tool_sha256": sha(binpath / "pkg-config"),
        "existing_compiled_dependencies_reused": False,
        "private_cache_path_recorded": False, "assertions_disabled": False,
        "source_remap": "/mnt/a=/quest3d", "openssl_build_info_policy": "Compiler command paths omitted from diagnostic metadata; actual build flags remain in recipe.",
        "built": False, "installed": False, "published": False,
    }
    (output / "private-path-build-preparation.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return {"prepared": True, "source_files": len(provenance["source_inputs"]), "overlay_ports": len(PACKAGES), "compiled_dependencies_reused": False}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--resume-tool-copy", action="store_true", help="Resume a failed tool copy before any source or build exists")
    parser.add_argument("--toolchain-ready", action="store_true", help="Verify existing pinned A-drive tool binaries without recopying tools")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.source, args.cache, args.output, args.repo,
                                resume_tool_copy=args.resume_tool_copy, toolchain_ready=args.toolchain_ready), indent=2))
    except Exception as error:
        # Do not echo local source/cache paths from exception text.
        if args.output.is_dir():
            detail = repr(error).encode("utf-8")
            if isinstance(error, subprocess.CalledProcessError):
                detail += b"\n" + (error.stderr or b"")
            (args.output / "preparation-error-private.log").write_bytes(detail)
        print(json.dumps({"prepared": False, "error_type": type(error).__name__}))
        raise SystemExit(1)
