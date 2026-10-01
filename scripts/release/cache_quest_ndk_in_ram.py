"""Optional bounded RAM read cache for pinned NDK tools during A-drive builds.

Mount mode runs as WSL root and creates a new, dedicated 8 GiB tmpfs. Apply
mode runs as the ordinary build user. All source/build/output files remain on
A; only unchanged NDK tools are cached in RAM. Original candidate tools are
preserved, and private source paths are never written to public receipts.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

CHECKS = ("source.properties", "build/cmake/android.toolchain.cmake",
          "toolchains/llvm/prebuilt/linux-x86_64/bin/clang",
          "toolchains/llvm/prebuilt/linux-x86_64/bin/ld.lld",
          "toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-ar",
          "toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip",
          "toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/aarch64-linux-android/libc++_shared.so")

def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as s:
        for b in iter(lambda: s.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()

def inventory(root: Path) -> dict:
    result = {}
    for p in root.rglob('*'):
        relative = p.relative_to(root).as_posix()
        if p.is_symlink():
            result[relative] = {'symlink': str(p.readlink())}
        elif p.is_file():
            result[relative] = {'sha256': sha(p), 'mode': stat.S_IMODE(p.stat().st_mode)}
    return result

def ram_path(path: Path) -> Path:
    if not re.fullmatch(r'/mnt/quest3d-native-build-tools-[0-9-]+', path.as_posix()):
        raise ValueError("Use a dedicated named Quest3D RAM cache")
    if path.is_symlink() or path.parent.resolve() != Path("/mnt"):
        raise ValueError("RAM cache parent cannot be a link")
    return path

def mount(root: Path, uid: int) -> dict:
    root = ram_path(root)
    if os.geteuid() != 0 or uid < 1 or root.exists():
        raise ValueError("Root mount mode requires a new path and ordinary owner UID")
    root.mkdir()
    subprocess.run(["mount", "-t", "tmpfs", "-o", "size=8G,noatime,nodev,nosuid", "tmpfs", str(root)],
                   check=True, capture_output=True)
    os.chown(root, uid, uid)
    return {"mounted": True, "maximum_ram_bytes": 8 * 1024**3}

def apply(base: Path, source: Path, root: Path) -> dict:
    base, source = base.resolve(strict=True), source.resolve(strict=True)
    root = ram_path(root)
    fs = subprocess.check_output(["findmnt", "-n", "-o", "FSTYPE", "--target", str(root)], text=True).strip()
    if not str(base).startswith('/mnt/a/') or fs != 'tmpfs':
        raise ValueError("A-drive build and an actual RAM filesystem required")
    target = root / "ndk"
    if target.exists() or '29.0.14206865' not in (source / 'source.properties').read_text():
        raise ValueError("New RAM target and pinned NDK r29 required")
    alias = base / 'tools/ndk'
    parent = alias.parent.resolve()
    if not parent.is_relative_to(base.parent) or alias.is_symlink():
        raise ValueError("Only an unchanged NDK inside this candidate family may be cached")
    backup = parent / 'ndk-before-ram-cache'
    if backup.exists():
        raise FileExistsError("Preserve the original tool backup")
    start = time.monotonic()
    subprocess.run(['cp', '-a', str(source), str(target)], check=True, capture_output=True)
    hashes = {n: sha(target / n) for n in CHECKS}
    if not all(sha(source / n) == h and sha(alias / n) == h for n, h in hashes.items()):
        raise ValueError("Pinned NDK input bytes differ")
    tree_start = time.monotonic()
    source_tree = inventory(source)
    if source_tree != inventory(target):
        raise ValueError("Copied NDK source/RAM tree bytes or modes differ")
    tree_seconds = round(time.monotonic() - tree_start, 2)
    alias.rename(backup)
    alias.symlink_to(target, target_is_directory=True)
    probe = subprocess.run([str(alias / CHECKS[2]), '--version'], capture_output=True)
    if probe.returncode:
        raise ValueError("RAM-cached compiler probe failed")
    result = {"schema": 1, "kind": "optional-ndk-RAM-readcache", "toolchain_version": "29.0.14206865",
              "maximum_ram_bytes": 8 * 1024**3, "private_source_path_recorded": False,
              "new_C_drive_storage_bytes": 0, "source_and_build_output_drive": "A",
              "native_tool_command_alias_unchanged": True, "original_A_drive_NDK_preserved": True,
              "tool_byte_checks": hashes, "copy_seconds": round(time.monotonic() - start, 2),
              "complete_source_and_RAM_tree_match": True,
              "complete_tree_entries_checked": len(source_tree),
              "complete_tree_inventory_sha256": hashlib.sha256(json.dumps(source_tree, sort_keys=True).encode()).hexdigest(),
              "complete_tree_check_seconds": tree_seconds,
              "compiler_probe_exit_code": probe.returncode}
    (base / 'ndk-RAM-readcache.json').write_text(json.dumps(result, indent=2) + '\n')
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('mount', 'apply'), required=True)
    parser.add_argument('--ram-root', required=True, type=Path)
    parser.add_argument('--owner-uid', type=int)
    parser.add_argument('--base', type=Path)
    parser.add_argument('--source-ndk', type=Path)
    a = parser.parse_args()
    try:
        result = mount(a.ram_root, a.owner_uid) if a.mode == 'mount' else apply(a.base, a.source_ndk, a.ram_root)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({'RAM_cache_ready': False, 'error_type': type(e).__name__}))
        raise SystemExit(1)
