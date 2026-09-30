"""Exercise cache synchronization against real isolated Git repositories."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


@pytest.fixture
def sync_case(tmp_path):
    project = tmp_path / "project"
    source = project / "third_party/nightfall"
    scripts = project / "scripts/quest"
    cache = tmp_path / "cache"
    scripts.mkdir(parents=True)
    source.mkdir(parents=True)
    git(source, "init", "-q")
    git(source, "config", "core.autocrlf", "false")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    for name in ("a.txt", "b.txt"):
        (source / name).write_bytes(b"base\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "fixture")
    commit = git(source, "rev-parse", "HEAD").decode().strip()
    subprocess.run(["git", "clone", "-q", str(source), str(cache)], check=True)
    git(cache, "config", "core.autocrlf", "false")
    for name in ("a.txt", "b.txt"):
        (source / name).write_bytes(b"previous\n")
        (cache / name).write_bytes(b"previous\r\n")
    old = project / "old.patch"
    old.write_bytes(git(source, "diff", "--binary"))
    for name in ("a.txt", "b.txt"):
        (source / name).write_bytes(b"reviewed\n")
    (scripts / "nightfall-pc-sbs.patch").write_bytes(git(source, "diff", "--binary"))
    (scripts / "versions.lock.json").write_text(json.dumps({"nightfall": {"commit": commit}}))
    tool = scripts / "sync_reviewed_delta.py"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/quest/sync_reviewed_delta.py", tool)
    journal = tmp_path / "journal"
    command = [sys.executable, str(tool), "--previous-patch", str(old),
               "--previous-sha256", hashlib.sha256(old.read_bytes()).hexdigest(),
               "--cache-source", str(cache), "--journal", str(journal)]
    return cache, journal, command


def test_apply_preserves_exact_crlf_backup(sync_case):
    cache, journal, command = sync_case
    result = subprocess.run(command + ["--apply"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (cache / "a.txt").read_bytes() == b"reviewed\n"
    assert (journal / "before/a.txt").read_bytes() == b"previous\r\n"
    record = json.loads((journal / "journal.json").read_text())
    assert record["applied"] and all(item["state"] == "applied" for item in record["changes"])


def test_unrelated_last_file_blocks_all_writes(sync_case):
    cache, journal, command = sync_case
    (cache / "b.txt").write_bytes(b"unrelated user change\n")
    result = subprocess.run(command + ["--apply"], capture_output=True, text=True)
    assert result.returncode != 0 and "Unrelated cache modification" in result.stderr
    assert (cache / "a.txt").read_bytes() == b"previous\r\n"
    assert (cache / "b.txt").read_bytes() == b"unrelated user change\n"
    assert not journal.exists()


def test_check_only_does_not_modify_cache(sync_case):
    cache, journal, command = sync_case
    subprocess.run(command, check=True, capture_output=True)
    assert (cache / "a.txt").read_bytes() == b"previous\r\n"
    assert not json.loads((journal / "journal.json").read_text())["applied"]


def test_wrong_previous_hash_preserves_cache(sync_case):
    cache, journal, command = sync_case
    command[command.index("--previous-sha256") + 1] = "0" * 64
    result = subprocess.run(command + ["--apply"], capture_output=True, text=True)
    assert result.returncode != 0 and "SHA256 mismatch" in result.stderr
    assert (cache / "a.txt").read_bytes() == b"previous\r\n"
    assert not journal.exists()
