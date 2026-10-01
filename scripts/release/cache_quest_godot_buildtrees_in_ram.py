"""Cache a not-yet-started Godot C++ buildtree in bounded temporary RAM.

The same source archives, recipes, compiler aliases and flags are used. The
prepared A-drive pathname stays unchanged. Only this package's temporary
buildtree is cached; installed static archives and final SO remain on A.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root: Path) -> dict:
    result = {}
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = {'symlink': str(path.readlink())}
        elif path.is_file():
            result[name] = {'sha256': sha(path), 'mode': stat.S_IMODE(path.stat().st_mode)}
    return result


def cache(base: Path, ram: Path) -> dict:
    base = base.resolve(strict=True)
    if not str(base).startswith('/mnt/a/') or not re.fullmatch(r'/mnt/quest3d-native-build-tools-[0-9-]+', ram.as_posix()):
        raise ValueError('Use the prepared A-drive build and bounded dedicated RAM cache')
    if ram.is_symlink() or subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', '-T', str(ram)], text=True).strip() != 'tmpfs':
        raise ValueError('An actual dedicated tmpfs is required')
    available = shutil.disk_usage(ram).free
    if available < 4 * 1024**3:
        raise ValueError('At least 4 GiB temporary RAM capacity must remain')
    alias = base / 'buildtrees/godot-cpp'
    backup = base / 'buildtrees/godot-cpp-before-ram-cache'
    target = ram / 'godot-cpp-buildtree'
    if alias.is_symlink() or backup.exists() or target.exists() or not alias.is_dir():
        raise ValueError('Only an untouched Godot package buildtree may be cached')
    before = inventory(alias)
    if set(before) != {'arm64-android.vcpkg_abi_info.txt'}:
        raise ValueError('Godot must be at the ABI-only boundary before extraction or configure')
    log = (base / 'native-build-private.log').read_text(errors='replace')
    if re.search(r'^Building godot-cpp', log, re.M):
        raise ValueError('Godot build already started')
    for proc in Path('/proc').iterdir():
        if proc.name.isdigit():
            try:
                command = (proc / 'cmdline').read_bytes()
                if str(alias).encode() in command and b'python3\0-\0' not in command:
                    raise ValueError('An active process is using the Godot buildtree')
            except (FileNotFoundError, PermissionError):
                pass
    shutil.copytree(alias, target, symlinks=True)
    if inventory(target) != before:
        raise ValueError('Copied buildtree bytes/modes/symlinks differ')
    # No Godot process has started. The active FFmpeg package is untouched.
    alias.rename(backup)
    try:
        alias.symlink_to(target, target_is_directory=True)
    except Exception:
        backup.rename(alias)
        raise
    result = {
        'schema': 1, 'kind': 'prestart-Godot-C++-temporary-RAM-buildtree',
        'package': 'godot-cpp', 'prepared_A_drive_alias': 'buildtrees/godot-cpp',
        'phase_at_application': 'before-source-extraction-and-configure',
        'temporary_RAM_root': ram.as_posix(), 'available_RAM_bytes_before_copy': available,
        'maximum_RAM_bytes': 8 * 1024**3, 'original_ABI_only_directory_preserved': True,
        'prestart_input_bytes_modes_and_symlinks_verified': True, 'initial_input_inventory': before,
        'source_archives_and_port_recipe_unchanged': True,
        'compiler_alias_and_flags_unchanged': True, 'diagnostic_prefix_map': '/mnt/a=/quest3d',
        'private_source_path_recorded': False, 'new_C_drive_storage_bytes': 0,
        'final_installed_archives_and_SO_drive': 'A', 'temporary_buildtree_drive': 'RAM',
        'final_native_private_path_gate_required': True,
    }
    (base / 'godot-RAM-buildtree.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, type=Path)
    parser.add_argument('--ram-root', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(cache(args.base, args.ram_root), indent=2))
    except Exception as error:
        print(json.dumps({'RAM_buildtree_applied': False, 'error_type': type(error).__name__}))
        raise SystemExit(1)
