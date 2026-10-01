"""Prepare a local Git repository from a verified export; never contact GitHub.

The immutable export and its manifest stay separate from Git's line-ending
normalization. No credentials, original worktree history, or runtime state are
copied. Repository creation, push and releases remain separate operations.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

spec = importlib.util.spec_from_file_location("reviewed_export", Path(__file__).with_name("export_repository.py"))
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


def github_target(owner: str, name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", owner):
        raise ValueError("Invalid GitHub owner")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name) or name.endswith(".git"):
        raise ValueError("Invalid GitHub repository name")
    return f"https://github.com/{owner}/{name}.git"


def git(root: Path, *args: str) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_SYSTEM=os.devnull, GIT_ATTR_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.attributesFile=" + os.devnull,
                             "-c", "core.excludesFile=" + os.devnull,
                             "-c", "core.hooksPath=" + str(root / ".git/empty-hooks"),
                             "-C", str(root), *args], capture_output=True, check=False, env=environment)
    if result.returncode:
        # Git diagnostics may include local paths; this tool is local-only.
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def prepare(export: Path, output: Path, owner: str, name: str, user_id: int) -> dict:
    remote = github_target(owner, name)
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("Verified public GitHub user ID is required")
    export = export.absolute()
    output = output.absolute()
    if output.exists() or output.resolve().is_relative_to(export.resolve()):
        raise ValueError("Use a new staging directory outside the immutable export")
    # Do not accept reparse-point parents for a newly created destination.
    parent = output.parent
    while not parent.exists():
        parent = parent.parent
    for ancestor in (parent, *parent.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Staging directory parent cannot be a reparse point")
    publication.verify_repository(export, publication.default_private_markers())
    manifest_path = export / "distribution-manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    output.mkdir(parents=True)
    repository = output / "repository"
    repository.mkdir()
    empty_template = output / "empty-template"
    empty_template.mkdir()
    for path, entry in manifest["files"].items():
        source = export / path
        publication.bundle.assert_regular(source, export)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"Export changed during staging: {path}")
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # No GitHub authentication or remote commands are performed here.
    git(repository, "init", "--initial-branch=main", "--template=" + str(empty_template))
    git(repository, "config", "core.autocrlf", "false")
    git(repository, "config", "core.safecrlf", "false")
    empty_hooks = repository / ".git/empty-hooks"
    empty_hooks.mkdir()
    git(repository, "config", "core.hooksPath", str(empty_hooks))
    git(repository, "config", "user.name", owner)
    git(repository, "config", "user.email", f"{user_id}+{owner}@users.noreply.github.com")
    git(repository, "add", "--", ".")
    selected = set(manifest["files"])
    staged = {value.decode("utf-8") for value in git(repository, "ls-files", "-z").split(b"\0") if value}
    if staged != selected:
        raise ValueError("Git ignored required source files or added unexpected files")
    git(repository, "commit", "--no-gpg-sign", "-m", "Prepare Sterevi open-source preview")
    git(repository, "remote", "add", "origin", remote)
    commit = git(repository, "rev-parse", "HEAD").decode().strip()
    blobs = {}
    # Git may normalize text; record the exact public tree, not working copies.
    for value in git(repository, "ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if not value:
            continue
        info, path = value.split(b"\t", 1)
        mode, kind, sha = info.decode().split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("Unexpected Git tree object")
        public_path = path.decode("utf-8")
        data = git(repository, "cat-file", "blob", sha)
        findings = publication.content_findings(public_path, data, publication.default_private_markers())
        if findings:
            raise ValueError("Git tree audit failed: " + json.dumps(findings))
        blobs[public_path] = {"git_blob": sha, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    if set(blobs) != selected:
        raise ValueError("Committed tree differs from reviewed source selection")
    report = {
        "schema": 1, "kind": "local-github-staging", "owner": owner, "name": name,
        "remote": remote, "branch": "main", "commit": commit,
        "export_manifest_sha256": publication.bundle.digest(manifest_path),
        "private_email_included": False, "uploaded": False, "repository_created": False,
        "native_corresponding_source_complete": False, "files": blobs,
    }
    (output / "git-tree-review.json").write_text(json.dumps(report, indent=2) + "\n", "utf-8")
    return {key: value for key, value in report.items() if key != "files"} | {"files": len(blobs)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--name", default="Sterevi")
    parser.add_argument("--github-user-id", type=int, required=True)
    args = parser.parse_args(argv)
    if not shutil.which("git"):
        raise RuntimeError("Git is required for local source staging")
    print(json.dumps(prepare(args.export, args.output, args.owner, args.name, args.github_user_id), indent=2))


if __name__ == "__main__":
    main()
