"""Exercise only owned miniature Git repositories; never use the shared build cache."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/quest"
spec = importlib.util.spec_from_file_location("update_patch", SCRIPTS / "update_patch.py")
update = importlib.util.module_from_spec(spec)
sys.modules["update_patch"] = update
spec.loader.exec_module(update)
spec = importlib.util.spec_from_file_location("sync_reviewed_delta_test", SCRIPTS / "sync_reviewed_delta.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

UI = "src/ui_controller.gd"
SCREEN = "src/screen_manager.gd"
AUDIO = "src/audio.bin"
NEW_TEST = "test/test_stream_resolution.gd"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def setup(tmp_path):
    project = tmp_path / "project"
    source = project / "third_party/nightfall"
    source.mkdir(parents=True)
    git(source, "init")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "config", "user.name", "Owned Fixture")
    git(source, "config", "core.autocrlf", "true")
    (source / "src").mkdir()
    (source / ".gitattributes").write_bytes(b"*.gd text eol=lf\n*.bin -text\n")
    (source / UI).write_bytes(b"base UI\n")
    (source / SCREEN).write_bytes(b"base screen\n")
    (source / AUDIO).write_bytes(b"\0base audio\xff")
    git(source, "add", ".")
    git(source, "commit", "-m", "fixture pinned base")
    commit = git(source, "rev-parse", "HEAD").decode().strip()
    cache = tmp_path / "private-cache"
    git(source, "clone", "--no-hardlinks", str(source), str(cache))
    scripts = project / "scripts/quest"
    scripts.mkdir(parents=True)
    (scripts / "versions.lock.json").write_text(json.dumps({"nightfall": {"commit": commit}}))
    (source / UI).write_bytes(b"deployed UI\n")
    (source / AUDIO).write_bytes(b"\0deployed audio\xff")
    (source / "test").mkdir()
    (source / NEW_TEST).write_bytes(b"extends Node\n# deployed test\n")
    baseline = tmp_path / "baseline.patch"
    added = subprocess.run(["git", "-C", str(source), "diff", "--no-index", "--", "/dev/null", NEW_TEST], capture_output=True)
    assert added.returncode == 1
    baseline.write_bytes(git(source, "diff", "--binary", commit) + added.stdout)
    git(cache, "apply", str(baseline))
    canonical = scripts / "nightfall-pc-sbs.patch"
    canonical.write_bytes(baseline.read_bytes())
    (source / UI).write_bytes(b"new quality UI\r\n")
    (source / NEW_TEST).write_bytes(b"extends Node\r\n# new quality test\r\n")
    (source / AUDIO).write_bytes(b"\0UNDEPLOYED FILE AUDIO\xff")
    (source / "new_file_audio.cpp").write_text("untracked future audio\n")
    return dict(project=project, source=source, cache=cache, commit=commit, baseline=baseline,
                canonical=canonical, candidate=tmp_path / "candidate.patch", journal=tmp_path / "journal")


def create(setup, selected=(UI, SCREEN, NEW_TEST)):
    args = ["--base-patch", str(setup["baseline"]), "--base-sha256", digest(setup["baseline"].read_bytes()),
            "--output", str(setup["candidate"])]
    for name in selected:
        args += ["--select", name]
    update.main(args, project=setup["project"])


def synchronize(setup, *, apply=True, previous_hash=None, current_hash=None, selected=(UI, SCREEN, NEW_TEST)):
    args = ["--previous-patch", str(setup["baseline"]), "--previous-sha256", previous_hash or digest(setup["baseline"].read_bytes()),
            "--current-patch", str(setup["candidate"]), "--current-sha256", current_hash or digest(setup["candidate"].read_bytes()),
            "--cache-source", str(setup["cache"]), "--journal", str(setup["journal"])]
    for name in selected:
        args += ["--select", name]
    if apply:
        args.append("--apply")
    sync.main(args, project=setup["project"])


def cache_files(setup):
    return {p.relative_to(setup["cache"]).as_posix(): p.read_bytes()
            for p in setup["cache"].rglob("*") if p.is_file() and ".git" not in p.relative_to(setup["cache"]).parts}


def test_selected_patch_and_sync_exclude_current_file_audio(setup):
    before = cache_files(setup)
    source_files = {name: (setup["source"] / name).read_bytes() for name in (UI, SCREEN, AUDIO, NEW_TEST, "new_file_audio.cpp")}
    staged = git(setup["source"], "diff", "--cached", "--binary")
    create(setup)
    previous = dict(update.patch_blocks(setup["baseline"].read_bytes()))
    candidate = dict(update.patch_blocks(setup["candidate"].read_bytes()))
    assert candidate[AUDIO] == previous[AUDIO]  # Exact binary Git block preservation.
    assert b"UNDEPLOYED" not in setup["candidate"].read_bytes()
    assert b"new_file_audio" not in setup["candidate"].read_bytes()
    assert b"\r" not in candidate[UI]  # Real Git CRLF clean normalization.
    assert b"\r" not in candidate[NEW_TEST]
    synchronize(setup)
    after = cache_files(setup)
    assert after[UI] == b"new quality UI\n"
    assert after[NEW_TEST] == b"extends Node\n# new quality test\n"
    assert {name: data for name, data in after.items() if name not in (UI, NEW_TEST)} == {name: data for name, data in before.items() if name not in (UI, NEW_TEST)}
    assert setup["canonical"].read_bytes() == setup["baseline"].read_bytes()
    assert source_files == {name: (setup["source"] / name).read_bytes() for name in source_files}
    assert git(setup["source"], "diff", "--cached", "--binary") == staged
    journal = json.loads((setup["journal"] / "journal.json").read_text())
    assert journal["applied"] and journal["write_source"] == "reconstructed-current-patch"
    assert {item["file"] for item in journal["changes"]} == {UI, NEW_TEST}
    assert journal["verified_unselected"][0]["file"] == AUDIO
    assert (setup["journal"] / "before" / UI).read_bytes() == before[UI]


def test_dry_run_does_not_change_any_cache_bytes(setup):
    create(setup)
    before = cache_files(setup)
    synchronize(setup, apply=False)
    assert cache_files(setup) == before
    assert not json.loads((setup["journal"] / "journal.json").read_text())["applied"]


@pytest.mark.parametrize("field", ["previous", "current"])
def test_sync_requires_exact_patch_hashes_before_writes(setup, field):
    create(setup)
    before = cache_files(setup)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        synchronize(setup, **{field + "_hash": "0" * 64})
    assert cache_files(setup) == before and not setup["journal"].exists()


def test_update_requires_exact_baseline_hash(setup):
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        update.main(["--base-patch", str(setup["baseline"]), "--base-sha256", "0" * 64,
            "--output", str(setup["candidate"]), "--select", UI], project=setup["project"])
    assert not setup["candidate"].exists()


@pytest.mark.parametrize("path", [AUDIO, "../src/ui_controller.gd", "src\\ui_controller.gd"])
def test_selection_outside_quality_allowlist_is_refused(setup, path):
    with pytest.raises(ValueError, match="allowed quality"):
        create(setup, selected=(path,))
    assert not setup["candidate"].exists()


def test_changed_unselected_patch_block_is_refused(setup):
    create(setup)
    # A syntactically valid other audio diff is not authorized by its hash alone.
    parts = dict(update.patch_blocks(git(setup["source"], "diff", "--binary", setup["commit"])))
    candidate = [(name, parts[AUDIO] if name == AUDIO else block)
                 for name, block in update.patch_blocks(setup["candidate"].read_bytes())]
    setup["candidate"].write_bytes(b"".join(block for _, block in candidate))
    before = cache_files(setup)
    with pytest.raises(ValueError, match="unselected patch"):
        synchronize(setup)
    assert cache_files(setup) == before and not setup["journal"].exists()


@pytest.mark.parametrize("path", [AUDIO, SCREEN])
def test_unrelated_cache_change_is_not_overwritten(setup, path):
    create(setup)
    (setup["cache"] / path).write_bytes(b"unrelated cache edit\n")
    before = cache_files(setup)
    with pytest.raises(ValueError, match="[Uu]n.*cache"):
        synchronize(setup)
    assert cache_files(setup) == before and not setup["journal"].exists()


def test_selected_working_change_after_patch_is_refused(setup):
    create(setup)
    (setup["source"] / UI).write_bytes(b"later unreviewed edit\n")
    before = cache_files(setup)
    with pytest.raises(ValueError, match="source changed"):
        synchronize(setup)
    assert cache_files(setup) == before and not setup["journal"].exists()


def test_selected_return_to_pinned_base_removes_old_diff(setup):
    (setup["source"] / UI).write_bytes(b"base UI\r\n")
    create(setup)
    assert UI not in dict(update.patch_blocks(setup["candidate"].read_bytes()))
    synchronize(setup)
    assert (setup["cache"] / UI).read_bytes() == b"base UI\n"


def test_wrong_pinned_source_commit_refused_without_patch_write(setup):
    path = setup["project"] / "scripts/quest/versions.lock.json"
    path.write_text(json.dumps({"nightfall": {"commit": "0" * 40}}))
    with pytest.raises(ValueError, match="unexpected Nightfall commit"):
        create(setup)
    assert not setup["candidate"].exists()


def test_canonical_or_existing_output_is_not_overwritten(setup):
    before = setup["canonical"].read_bytes()
    setup["candidate"] = setup["canonical"]
    with pytest.raises(ValueError, match="must be new"):
        create(setup)
    assert setup["canonical"].read_bytes() == before


def test_untracked_selection_requires_existing_baseline_block(setup):
    name = "test/test_screen_presentation_controls.gd"
    (setup["source"] / name).write_text("extends Node\n")
    with pytest.raises(ValueError, match="not in the reviewed baseline"):
        create(setup, selected=(name,))
    assert not setup["candidate"].exists()
