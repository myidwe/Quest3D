"""Git staging must preserve the export boundary and never upload private state."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("github_staging", ROOT / "scripts/release/prepare_github.py")
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


def export_fixture(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    for name in staging.publication.ROOT_FILES:
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("public source\n", "utf-8")
    (source / ".gitignore").write_text(".cache/\n", "utf-8")
    (source / ".gitattributes").write_text("* text=auto\n*.py text eol=lf\n", "utf-8")
    for name in staging.publication.PUBLIC_DOCS:
        file = source / "docs" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("documentation\n", "utf-8")
    for name in staging.publication.SOURCE_TREES:
        (source / name).mkdir(parents=True, exist_ok=True)
    (source / "src/quest3d/example.py").write_bytes(b"# reviewed\r\nprint('hello')\r\n")
    (source / "config/desktop.json").write_text("private runtime")
    export = tmp_path / "export"
    staging.publication.export_repository(source, export)
    return source, export


@pytest.mark.skipif(not shutil.which("git"), reason="Git executable required")
def test_local_git_staging_records_exact_tree_without_private_email_or_upload(tmp_path):
    source, export = export_fixture(tmp_path)
    output = tmp_path / "stage"
    before = (export / "distribution-manifest.json").read_bytes()
    result = staging.prepare(export, output, "example-owner", "Quest3D", 123456)
    repo = output / "repository"
    assert not result["uploaded"] and not result["repository_created"]
    assert not result["native_corresponding_source_complete"]
    assert staging.git(repo, "remote", "get-url", "origin").strip() == b"https://github.com/example-owner/Quest3D.git"
    assert staging.git(repo, "log", "-1", "--format=%ae").strip() == b"123456+example-owner@users.noreply.github.com"
    assert staging.git(repo, "status", "--porcelain") == b""
    assert (export / "distribution-manifest.json").read_bytes() == before
    assert not (source / ".git").exists()
    assert not (repo / "config/desktop.json").exists()
    assert not (repo / "distribution-manifest.json").exists()
    report = json.loads((output / "git-tree-review.json").read_text())
    assert report["files"]["src/quest3d/example.py"]["bytes"] == len(b"# reviewed\nprint('hello')\n")
    assert staging.git(repo, "show", "HEAD:src/quest3d/example.py") == b"# reviewed\nprint('hello')\n"


def test_tampered_export_is_rejected_before_creating_git_directory(tmp_path):
    _, export = export_fixture(tmp_path)
    (export / "README.md").write_text("unreviewed change")
    output = tmp_path / "stage"
    with pytest.raises(ValueError, match="hash mismatch"):
        staging.prepare(export, output, "example-owner", "Quest3D", 123456)
    assert not output.exists()


@pytest.mark.parametrize("owner,name", [("../owner", "repo"), ("owner", "../repo"), ("owner", "repo.git"), ("--switch", "repo")])
def test_remote_name_cannot_be_a_path_or_command(owner, name):
    with pytest.raises(ValueError):
        staging.github_target(owner, name)


def test_staging_cannot_mutate_immutable_export(tmp_path):
    _, export = export_fixture(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        staging.prepare(export, export / "git", "example-owner", "Quest3D", 123456)
    assert not (export / "git").exists()


@pytest.mark.skipif(not shutil.which("git"), reason="Git executable required")
def test_required_source_ignored_by_git_is_not_committed(tmp_path):
    source, _ = export_fixture(tmp_path)
    (source / ".gitignore").write_text("src/\n", "utf-8")
    export = tmp_path / "ignored-export"
    staging.publication.export_repository(source, export)
    output = tmp_path / "stage"
    with pytest.raises(ValueError, match="ignored required"):
        staging.prepare(export, output, "example-owner", "Quest3D", 123456)
    assert staging.git(output / "repository", "log", "--all", "--oneline") == b""


@pytest.mark.skipif(not shutil.which("git"), reason="Git executable required")
def test_staging_does_not_inherit_external_worktree_or_global_exclusion_config(tmp_path, monkeypatch):
    source, export = export_fixture(tmp_path)
    global_config = tmp_path / "global-config"
    excluded = tmp_path / "global-ignore"
    excluded.write_text("*\n")
    global_config.write_text("[core]\n\texcludesFile = " + excluded.as_posix() + "\n")
    monkeypatch.setenv("GIT_WORK_TREE", str(source))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    result = staging.prepare(export, tmp_path / "stage", "example-owner", "Quest3D", 123456)
    assert result["files"] > 1 and not result["uploaded"]
    assert not (source / ".git").exists()
