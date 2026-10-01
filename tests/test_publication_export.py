"""A source-review export must not copy personal state or silently ignore secrets."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("publication_export", ROOT / "scripts/release/export_repository.py")
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


def minimal_tree(root):
    for name in publication.ROOT_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public source\n", "utf-8")
    for name in publication.PUBLIC_DOCS:
        path = root / "docs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public documentation\n", "utf-8")
    for name in publication.SOURCE_TREES:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "src/quest3d/app.py").write_text("print('fixture')\n", "utf-8")


def test_export_leaves_workspace_and_state_outside_candidate(tmp_path):
    source = tmp_path / "input"
    minimal_tree(source)
    (source / "config/desktop.json").write_text("private preferences")
    (source / "scripts/quest/install-reviewed.ps1").write_text("private device operation")
    (source / "artifacts").mkdir()
    (source / "artifacts/user.py").write_text("private diagnostic")
    (source / "native/capture/vendor").mkdir(parents=True)
    (source / "native/capture/vendor/experimental.rs").write_text("excluded dependency")
    candidate = tmp_path / "repository"
    result = publication.export_repository(source, candidate)
    assert result["verified"] and not result["native_corresponding_source_complete"]
    assert not (candidate / "config/desktop.json").exists()
    assert not (candidate / "scripts/quest/install-reviewed.ps1").exists()
    assert not (candidate / "artifacts").exists()
    assert not (candidate / "native/capture/vendor").exists()
    assert not (source / ".git").exists()
    assert (source / "config/desktop.json").read_text() == "private preferences"


def test_sensitive_content_fails_before_any_export_and_does_not_print_it(tmp_path):
    source = tmp_path / "input"
    minimal_tree(source)
    sensitive = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5
    (source / "src/quest3d/app.py").write_text(sensitive)
    candidate = tmp_path / "repository"
    with pytest.raises(ValueError) as error:
        publication.export_repository(source, candidate)
    assert "private-key" in str(error.value)
    assert sensitive not in str(error.value)
    assert not candidate.exists()


def test_private_marker_does_not_expose_matched_value(tmp_path):
    source = tmp_path / "input"
    minimal_tree(source)
    (source / "src/quest3d/app.py").write_text("personal-device-marker")
    with pytest.raises(ValueError) as error:
        publication.export_repository(source, tmp_path / "repository", ("personal-device-marker",))
    assert "private-marker" in str(error.value)
    assert "personal-device-marker" not in str(error.value)


def test_binary_resource_utf16_private_marker_is_not_skipped():
    marker = "synthetic-owner-account"
    data = b"\x89PNG\r\n\x1a\n" + marker.encode("utf-16le")
    findings = publication.content_findings("resources/icon.png", data, (marker,))
    assert any(item["kind"].startswith("private-marker") for item in findings)
    assert marker not in str(findings)


def test_export_rejects_existing_destination_and_tamper(tmp_path):
    source = tmp_path / "input"
    minimal_tree(source)
    candidate = tmp_path / "repository"
    publication.export_repository(source, candidate)
    with pytest.raises(FileExistsError):
        publication.export_repository(source, candidate)
    (candidate / "src/quest3d/app.py").write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        publication.verify_repository(candidate)


def test_source_changed_after_audit_is_not_copied_as_reviewed(tmp_path, monkeypatch):
    source = tmp_path / "input"
    minimal_tree(source)
    original = publication.bundle.Payload.copy

    def change_during_copy(payload, file, name, expected=None):
        if name == "src/quest3d/app.py":
            file.write_text("unreviewed change")
        return original(payload, file, name, expected)

    monkeypatch.setattr(publication.bundle.Payload, "copy", change_during_copy)
    candidate = tmp_path / "repository"
    with pytest.raises(ValueError, match="Pinned hash mismatch"):
        publication.export_repository(source, candidate)
    assert not (candidate / "src/quest3d/app.py").exists()
    assert not (candidate / "distribution-manifest.json").exists()


@pytest.mark.parametrize("prefix,kind", [("hf" + "_", "huggingface-token"), ("sk" + "-proj-", "openai-token")])
def test_additional_tokens_are_redacted_from_finding(prefix, kind):
    token = prefix + "A" * 50
    findings = publication.content_findings("config/example.txt", token.encode())
    assert findings == [{"file": "config/example.txt", "kind": kind}]
    assert token not in str(findings)
