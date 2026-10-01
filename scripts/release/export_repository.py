"""Export a reviewed source repository without initializing Git or uploading files.

The result is a source-review candidate, not a complete native release source
bundle or an installer. Keep the private development workspace separate.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location("publication_bundle", Path(__file__).with_name("build_bundle.py"))
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)
ROOT = Path(__file__).resolve().parents[2]
ROOT_FILES = (
    "README.md", "README.en.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md",
    "SECURITY.md", "CHANGELOG.md", ".gitignore", ".gitattributes",
    ".python-version", "pyproject.toml", "uv.lock", "config/models.json",
    "scripts/install-desktop-shortcut.ps1", "scripts/stop-verified-host.py",
    "scripts/build-host.ps1", "scripts/build-quest.ps1",
    "scripts/prepare-quest-public-ui-build.py",
)
PUBLIC_DOCS = (
    "DISTRIBUTION.md", "BUILDING.md", "DESKTOP_USER_GUIDE.md", "DESKTOP_USER_GUIDE.html",
    "OPEN_SOURCE_RELEASE_PLAN_2026-09-30.md", "DEPENDENCY_AUDIT_2026-09-30.md",
    "PRODUCT_SCOPE.md", "RELEASE_NOTES_TEMPLATE.md", "GITHUB_PUBLICATION.md",
    "HOST_RELEASE_PREPARATION_2026-09-30.md", "QUEST_RELEASE_PREPARATION_2026-09-30.md",
    "SETUP_PERMISSIONS_2026-09-30.md",
    "QUEST_CLEAN_BUILD_2026-10-01.md",
    "PUBLIC_RELEASE_CHECKLIST_2026-10-01.md",
    "RELEASE_0.1.0_PREVIEW.md",
    "QUEST_PUBLIC_UI_REFINEMENT_2026-10-01.md",
)
SOURCE_TREES = ("src/quest3d", "resources", "native", "scripts/release", "scripts/quest", "patches", "tests", ".github")
EXCLUDED_FILES = set(bundle.LOCAL_ONLY_DIAGNOSTICS) | {
    "dev-firewall.ps1", "HDR_CANDIDATE.md", "WINDOW_CANDIDATE.md",
}
BINARY_ASSETS = {".png", ".ico", ".otf", ".ttf"}
SECRET_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "huggingface-token": re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    "openai-token": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b"),
}


def default_private_markers() -> tuple[str, ...]:
    home = str(Path.home())
    values = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
    # WSL developer scripts occasionally embed the same account's Linux home.
    values.add("/home/" + Path.home().name + "/")
    return tuple(value.casefold() for value in values)


def content_findings(name: str, data: bytes, private_markers=()) -> list[dict]:
    """Report the location/type only; never print matched sensitive contents."""
    if Path(name).suffix.casefold() in BINARY_ASSETS:
        return []
    value = data.decode("utf-8-sig", errors="replace")
    findings = [{"file": name, "kind": kind} for kind, pattern in SECRET_PATTERNS.items() if pattern.search(value)]
    if any(marker and marker.casefold() in value.casefold() for marker in private_markers):
        findings.append({"file": name, "kind": "private-marker"})
    return findings


def selected_files(root: Path) -> list[str]:
    selected = list(ROOT_FILES) + ["docs/" + name for name in PUBLIC_DOCS]
    for tree in SOURCE_TREES:
        source = root / tree
        if not source.is_dir():
            raise FileNotFoundError(f"Required source directory: {tree}")
        for parent, dirs, files in os.walk(source, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in (bundle.SKIP_DIRS | {"vendor"})
                             and not name.startswith("cmake-build-"))
            for name in sorted(files):
                path = Path(parent) / name
                if name in EXCLUDED_FILES or any(fnmatch.fnmatchcase(name, pattern) for pattern in bundle.LOCAL_ONLY_SCRIPTS):
                    continue
                if name.casefold() in bundle.FORBIDDEN_NAMES or path.suffix.casefold() in bundle.FORBIDDEN_SUFFIXES:
                    continue
                if path.suffix.casefold() not in bundle.SOURCE_SUFFIXES and name not in {"LICENSE", "COPYING", "NOTICE", "CMakeLists.txt", "Cargo.lock"}:
                    continue
                selected.append(path.relative_to(root).as_posix())
    if len(selected) != len(set(selected)):
        raise ValueError("Duplicate source selection")
    return sorted(selected)


def audit_files(root: Path, names, private_markers=()) -> dict[str, str]:
    findings = []
    checksums = {}
    for name in names:
        bundle.public_name(name)
        source = root / name
        bundle.assert_regular(source, root)
        if source.stat().st_size >= 100 * 1024 * 1024:
            findings.append({"file": name, "kind": "oversize-git-file"})
        else:
            data = source.read_bytes()
            findings.extend(content_findings(name, data, private_markers))
            checksums[name] = hashlib.sha256(data).hexdigest()
    if findings:
        raise ValueError("Publication content audit failed: " + json.dumps(findings, ensure_ascii=True))
    return checksums


def export_repository(root: Path, output: Path, private_markers=()) -> dict:
    root = root.resolve()
    names = selected_files(root)
    # Validate the entire selection before creating/copying the candidate.
    checksums = audit_files(root, names, private_markers)
    payload = bundle.Payload(root, output)
    for name in names:
        payload.copy(root / name, name, checksums[name])
    payload.manifest(release="unreleased-source-review", metadata={
        "kind": "repository-review-candidate", "published": False,
        "installer_included": False, "model_weights_included": False,
        "native_corresponding_source_complete": False,
        "release_gate": "docs/OPEN_SOURCE_RELEASE_PLAN_2026-09-30.md",
    })
    return verify_repository(output, private_markers)


def verify_repository(output: Path, private_markers=()) -> dict:
    manifest = bundle.verify_directory(output)
    if manifest["metadata"].get("kind") != "repository-review-candidate":
        raise ValueError("Not a repository review candidate")
    audit_files(output, manifest["files"], private_markers)
    return {"verified": True, "files": len(manifest["files"]),
            "bytes": sum(value["bytes"] for value in manifest["files"].values()),
            "published": False, "native_corresponding_source_complete": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    markers = default_private_markers()
    result = verify_repository(args.output, markers) if args.verify else export_repository(args.root, args.output, markers)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
