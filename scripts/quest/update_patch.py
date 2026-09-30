"""Record Nightfall changes, or replace only allowed quality files in a pinned patch."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess


QUALITY_FILES = frozenset({
    "src/ui_controller.gd", "src/performance_telemetry.gd",
    "src/settings_controller.gd", "src/stream_manager.gd",
    "src/screen_manager.gd", "src/native_xr_renderer.gd",
    "test/test_screen_presentation_controls.gd", "test/test_stream_resolution.gd",
})


def selected_files(names: list[str]) -> set[str]:
    selected = set(names)
    if not selected or len(selected) != len(names) or not selected <= QUALITY_FILES:
        raise ValueError("Select each allowed quality path exactly once; other paths are refused")
    return selected


def patch_blocks(data: bytes) -> list[tuple[str, bytes]]:
    """The pinned patch uses plain same-path Git blocks; reject ambiguous headers."""
    if not data:
        return []
    starts = [match.start() for match in re.finditer(rb"(?m)^diff --git ", data)]
    if not starts or starts[0] != 0:
        raise ValueError("Expected a plain Git patch without a preamble")
    result = []
    for begin, end in zip(starts, starts[1:] + [len(data)]):
        block = data[begin:end]
        header = re.fullmatch(rb"diff --git a/([^\s]+) b/([^\s]+)\r?\n", block.splitlines(keepends=True)[0])
        if header is None or header[1] != header[2]:
            raise ValueError("Quoted/renamed patch paths require separate review")
        name = header[1].decode("utf-8")
        part = Path(name)
        if part.is_absolute() or ".." in part.parts or "\\" in name or ":" in name or name.startswith("/"):
            raise ValueError(f"Unsafe patch path: {name}")
        if name in {existing for existing, _ in result}:
            raise ValueError(f"Duplicate patch path: {name}")
        result.append((name, block))
    return result


def preserve_unselected(previous: bytes, current: bytes, selected: set[str]) -> None:
    old = {name: block for name, block in patch_blocks(previous) if name not in selected}
    new = {name: block for name, block in patch_blocks(current) if name not in selected}
    if old != new:
        raise ValueError("The candidate changed an unselected patch block")


def main(argv=None, *, project: Path | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-patch", type=Path)
    parser.add_argument("--base-sha256")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--select", action="append", default=[], metavar="QUALITY_PATH")
    args = parser.parse_args(argv)
    project = project or Path(__file__).resolve().parents[2]
    directory = project / "scripts/quest"
    source = project / "third_party/nightfall"
    expected = json.loads((directory / "versions.lock.json").read_text())["nightfall"]["commit"]
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != expected:
        raise ValueError(f"Refusing patch from unexpected Nightfall commit {actual}")
    if args.select:
        selected = selected_files(args.select)
        if args.base_patch is None or args.base_sha256 is None or args.output is None:
            raise ValueError("Selection requires --base-patch, --base-sha256 and a new --output")
        previous = args.base_patch.read_bytes()
        if hashlib.sha256(previous).hexdigest() != args.base_sha256.lower():
            raise ValueError("Base reviewed patch SHA256 mismatch")
        output = args.output.resolve()
        if output.exists() or output == (directory / "nightfall-pc-sbs.patch").resolve():
            raise ValueError("Selection output must be new and cannot be the canonical patch")
        old_blocks = dict(patch_blocks(previous))
        added = {}
        for name in selected:
            file = source / name
            if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(source.resolve()):
                raise ValueError(f"Selected file is absent or unsafe: {name}")
            data = file.read_bytes()
            data.decode("utf-8")
            if b"\0" in data:
                raise ValueError(f"Selected GDScript must be UTF-8 text: {name}")
            exists = subprocess.run(["git", "-C", str(source), "cat-file", "-e", f"{expected}:{name}"], capture_output=True)
            if exists.returncode:
                if name not in old_blocks or b"\nnew file mode 100644\n" not in old_blocks[name]:
                    raise ValueError(f"Selected new file was not in the reviewed baseline: {name}")
                # No-index still uses Git's source-path clean conversion on the
                # actual working file. It does not stage/write a Git object.
                result = subprocess.run(["git", "-C", str(source), "diff", "--no-ext-diff",
                    "--no-textconv", "--no-index", "--binary", "--", "/dev/null", name], capture_output=True)
                if result.returncode not in (0, 1):
                    raise ValueError(f"Git could not normalize selected new file: {name}")
                added.update(patch_blocks(result.stdout))
        # Git supplies clean/filter-normalized working-tree bytes, including CRLF
        # conversion. Diffing against the pinned commit includes staged edits.
        replacement = dict(patch_blocks(subprocess.check_output([
            "git", "-C", str(source), "diff", "--no-ext-diff", "--no-textconv",
            "--binary", "--no-renames", expected, "--", *sorted(selected)])))
        replacement.update(added)
        if not set(replacement) <= selected:
            raise ValueError("Git returned a diff outside the selected files")
        if any(re.search(rb"(?m)^(old mode|new mode) ", block) for block in replacement.values()):
            raise ValueError("Selected file mode changes require separate review")
        blocks = []
        remaining = dict(replacement)
        for name, block in patch_blocks(previous):
            if name in selected:
                block = remaining.pop(name, b"")  # A return to the base removes this block.
            blocks.append(block)
        blocks.extend(remaining[name] for name in sorted(remaining))
        candidate = b"".join(blocks)
        preserve_unselected(previous, candidate, selected)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            stream.write(candidate)
        print(json.dumps({"base_commit": expected, "base_patch_sha256": args.base_sha256.lower(),
            "patch_sha256": hashlib.sha256(candidate).hexdigest(), "files": len(patch_blocks(candidate)),
            "selected": sorted(selected), "output": str(output), "unselected_blocks_preserved": True}))
        return
    if args.base_patch is not None or args.base_sha256 is not None or args.output is not None:
        raise ValueError("Base/output options require explicit --select")
    # Preserve the existing full-update command; callers wanting a quality-only
    # build must explicitly use selection mode above.
    patch = subprocess.check_output(["git", "-C", str(source), "diff", "--binary"]).decode()
    new_files = subprocess.check_output(["git", "-C", str(source), "ls-files", "--others", "--exclude-standard"], text=True).splitlines()
    for name in new_files:
        content = (source / name).read_text(encoding="utf-8").splitlines(keepends=True)
        patch += f"diff --git a/{name} b/{name}\nnew file mode 100644\n"
        patch += "".join(difflib.unified_diff([], content, fromfile="/dev/null", tofile="b/" + name))
    (directory / "nightfall-pc-sbs.patch").write_text(patch, encoding="utf-8", newline="\n")
    print("Updated Nightfall patch; local source and staging preserved")


if __name__ == "__main__":
    main()
