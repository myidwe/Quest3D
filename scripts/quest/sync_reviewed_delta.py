"""Sync an explicitly reviewed patch delta into the isolated Quest build cache.

Reconstruct the previous patch, compare every destination before writing anything,
and preserve exact previous bytes in a journal. Unrelated cache edits are refused.
Run in the existing WSL build environment; no checkout reset or broad copy occurs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from update_patch import patch_blocks, preserve_unselected, selected_files


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def patch_files(patch: Path) -> list[str]:
    return sorted(name for name, _ in patch_blocks(patch.read_bytes()))


def safe_target(root: Path, name: str) -> Path:
    target = root / name
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"Destination escapes cache: {name}")
    for path in [target, *target.parents]:
        if path == root:
            break
        if path.is_symlink():
            raise ValueError(f"Refusing symlink destination: {name}")
    return target


def main(argv=None, *, project: Path | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-patch", required=True, type=Path)
    parser.add_argument("--previous-sha256", required=True)
    parser.add_argument("--cache-source", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--current-patch", type=Path)
    parser.add_argument("--current-sha256")
    parser.add_argument("--select", action="append", default=[], metavar="QUALITY_PATH")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    project = project or Path(__file__).resolve().parents[2]
    source = project / "third_party/nightfall"
    current_patch = (args.current_patch or project / "scripts/quest/nightfall-pc-sbs.patch").resolve()
    previous_patch = args.previous_patch.resolve()
    previous_data = previous_patch.read_bytes()
    current_data = current_patch.read_bytes()
    if sha(previous_data) != args.previous_sha256.lower():
        raise ValueError("Previous reviewed patch SHA256 mismatch")
    selected = selected_files(args.select) if args.select else None
    if selected is not None:
        if args.current_patch is None or args.current_sha256 is None:
            raise ValueError("Selection requires --current-patch and --current-sha256")
        preserve_unselected(previous_data, current_data, selected)
    elif args.current_patch is not None or args.current_sha256 is not None:
        raise ValueError("Explicit current patch options require --select")
    if args.current_sha256 is not None and sha(current_data) != args.current_sha256.lower():
        raise ValueError("Current reviewed patch SHA256 mismatch")
    cache = args.cache_source.resolve(strict=True)
    if cache == source.resolve():
        raise ValueError("Build cache must be separate from reviewed source")
    commit = json.loads((project / "scripts/quest/versions.lock.json").read_text())["nightfall"]["commit"]
    for repo in (source, cache):
        actual = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        if actual != commit:
            raise ValueError(f"Unexpected base commit at {repo}")
    if args.journal.exists():
        raise ValueError("Journal destination already exists; preserve and inspect it")
    old_names = [name for name, _ in patch_blocks(previous_data)]
    names = sorted(set(old_names + [name for name, _ in patch_blocks(current_data)] + list(selected or [])))
    journal = {"schema": 1, "base_commit": commit, "previous_patch_sha256": args.previous_sha256.lower(),
               "current_patch_sha256": sha(current_data), "applied": False, "changes": []}
    if selected is not None:
        journal.update(selected=sorted(selected), verified_unselected=[],
                       write_source="reconstructed-current-patch", unselected_blocks_preserved=True)
    writes = []
    with tempfile.TemporaryDirectory(prefix="quest-reviewed-previous-") as temporary:
        old_root = Path(temporary) / "previous"
        new_root = Path(temporary) / "current"
        old_root.mkdir()
        if selected is not None:
            new_root.mkdir()
        old_patch_snapshot = Path(temporary) / "previous.patch"
        old_patch_snapshot.write_bytes(previous_data)
        new_patch_snapshot = Path(temporary) / "current.patch"
        new_patch_snapshot.write_bytes(current_data)
        for name in names:
            result = subprocess.run(["git", "-C", str(source), "show", f"{commit}:{name}"], capture_output=True)
            if result.returncode == 0:
                target = old_root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(result.stdout)
                if selected is not None:
                    target = new_root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(result.stdout)
        if previous_data:
            subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply", "--check", str(old_patch_snapshot)], cwd=old_root, check=True)
            subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply", str(old_patch_snapshot)], cwd=old_root, check=True)
        if selected is not None and current_data:
            subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply", "--check", str(new_patch_snapshot)], cwd=new_root, check=True)
            subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply", str(new_patch_snapshot)], cwd=new_root, check=True)
        for name in names:
            reviewed = (new_root if selected is not None else source) / name
            old = old_root / name
            if not reviewed.is_file():
                raise ValueError(f"Deletion requires explicit separate handling: {name}")
            new_data = reviewed.read_bytes()
            old_data = old.read_bytes() if old.is_file() else None
            if selected is not None:
                target = safe_target(cache, name)
                existing = target.read_bytes() if target.is_file() else None
                if name not in selected:
                    if old_data != new_data or existing is None or normalized(existing) != normalized(old_data):
                        raise ValueError(f"Unselected cache/patch modification preserved: {name}")
                    journal["verified_unselected"].append({"file": name, "sha256": sha(existing),
                        "normalized_sha256": sha(normalized(existing))})
                    continue
                working = safe_target(source.resolve(), name)
                if not working.is_file() or normalized(working.read_bytes()) != normalized(new_data):
                    raise ValueError(f"Selected source changed after patch review: {name}")
            if old_data is not None and normalized(old_data) == normalized(new_data):
                if selected is not None and (existing is None or normalized(existing) != normalized(old_data)):
                    raise ValueError(f"Unrelated cache modification preserved: {name}")
                continue
            target = safe_target(cache, name)
            existing = target.read_bytes() if target.is_file() else None
            if existing is not None and normalized(existing) == normalized(new_data):
                journal["changes"].append({"file": name, "state": "already-current", "sha256": sha(existing)})
                continue
            if (existing is None) != (old_data is None) or (existing is not None and normalized(existing) != normalized(old_data)):
                raise ValueError(f"Unrelated cache modification preserved: {name}")
            journal["changes"].append({"file": name, "state": "pending", "before_sha256": None if existing is None else sha(existing), "after_sha256": sha(new_data)})
            writes.append((name, target, existing, new_data))
    args.journal.mkdir(parents=True)
    journal_file = args.journal / "journal.json"
    journal_file.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")
    for name, target, existing, new_data in writes:
        if existing is not None:
            backup = args.journal / "before" / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(existing)
        if args.apply:
            observed = target.read_bytes() if target.is_file() else None
            if observed != existing:
                raise ValueError(f"Cache changed after preflight; journal preserved: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".quest-sync-", dir=target.parent, delete=False) as output:
                output.write(new_data)
                staged = Path(output.name)
            if target.exists():
                staged.chmod(target.stat().st_mode)
            staged.replace(target)
            if sha(target.read_bytes()) != sha(new_data):
                raise ValueError(f"Post-write verification failed: {name}")
            for item in journal["changes"]:
                if item["file"] == name:
                    item["state"] = "applied"
                    journal_file.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")
    journal["applied"] = args.apply
    journal_file.write_text(json.dumps(journal, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"applied": args.apply, "changed_files": len(writes), "journal": str(args.journal)}))


if __name__ == "__main__":
    main()
