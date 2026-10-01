"""Final bytes, not text-only exports, decide whether private data is shipped."""
import importlib.util
import io
import json
from pathlib import Path
import struct
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("release_privacy_audit", ROOT / "scripts/release/privacy_audit.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
OWNER = "private-fixture-identity"


def zipped(files, comment=b""):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
        archive.comment = comment
    return output.getvalue()


@pytest.mark.parametrize("magic", [b"\x7fELF", b"MZ\x90\x00", b"\x89PNG\r\n\x1a\n", b"OTTO"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16le", "utf-16be"])
def test_private_markers_are_found_in_binary_assets_and_both_utf16_orders(magic, encoding):
    verifier = audit.Auditor({"owner": [OWNER]})
    verifier.inspect("library.bin", magic + b"\x00\xff" + OWNER.upper().encode(encoding))
    report = verifier.report()
    assert not report["passed"]
    assert report["findings_by_category"]["private-marker:owner"] == 1
    assert report["findings"][0]["offsets"] == [6 if len(magic) == 4 else len(magic) + 2]
    assert OWNER not in json.dumps(report)


def test_nested_zip_is_opened_and_archive_filename_is_also_inspected():
    verifier = audit.Auditor({"owner": [OWNER]})
    nested = zipped({"lib/libvideo.so": b"\x7fELF" + OWNER.encode()})
    verifier.inspect("package.zip", zipped({"app.apk": nested, OWNER + ".txt": b"safe"}))
    report = verifier.report()
    assert report["counts"]["archives_opened"] == 2
    assert not report["passed"]
    assert any("libvideo.so" in f["path"] for f in report["findings"])
    assert any(f["path"].endswith("::archive-metadata") for f in report["findings"])
    assert OWNER not in json.dumps(report)


def test_tar_owner_metadata_and_links_are_inspected_without_extracting(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("../must-not-be-extracted")
        entry.uname = OWNER
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/" + "home/" + OWNER + "/target"
        archive.addfile(entry)
    verifier = audit.Auditor({"owner": [OWNER]})
    verifier.inspect("dependency.tar.gz", buffer.getvalue())
    assert not verifier.report()["passed"]
    assert verifier.counts["archives_opened"] == 1
    assert not (tmp_path.parent / "must-not-be-extracted").exists()


def test_token_in_utf16_is_blocked_with_correct_byte_offset():
    token = "gh" + "p_" + "A" * 36
    verifier = audit.Auditor({})
    verifier.inspect("app.exe", b"MZ" + ("prefix " + token).encode("utf-16le"))
    found = next(f for f in verifier.findings if f["category"] == "github-token")
    assert found["encoding"] == "utf-16le" and found["offsets"] == [16]
    assert found["status"] == "blocked"
    assert token not in json.dumps(verifier.report())


def test_tls_parser_label_is_not_mistaken_for_private_key_but_real_block_is_blocked():
    begin = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5
    end = "-" * 5 + "END RSA PRIVATE KEY" + "-" * 5
    verifier = audit.Auditor({})
    verifier.inspect("parser.so", (begin + "\x00RSA_LABEL" + end).encode())
    assert verifier.report()["passed"]
    verifier.inspect("key.pem", (begin + "\n" + "ABCD" * 64 + "\n" + end).encode())
    assert verifier.report()["findings_by_category"]["private-key-block"] == 1
    assert not verifier.report()["passed"]


def test_exact_public_origin_review_never_waives_owner_marker():
    data = ("/" + "home/publisher/src/" + OWNER).encode()
    review = {"sha256": audit.digest(data), "categories": ["generic-user-home-path", "private-marker:owner"],
              "origin": "https://example.org/pinned-source", "reason": "Exact public upstream bytes"}
    verifier = audit.Auditor({"owner": [OWNER]}, [review])
    verifier.inspect("dependency.c", data)
    assert not verifier.report()["passed"]
    assert next(f for f in verifier.findings if f["category"] == "private-marker:owner")["status"] == "blocked"
    assert next(f for f in verifier.findings if f["category"] == "generic-user-home-path")["status"] == "reviewed-public-origin"


def test_public_archive_review_is_exact_bytes_and_changes_cannot_reuse_it():
    key_name = "debug" + ".keystore"
    original = zipped({key_name: b"upstream test identity"})
    review = {"sha256": audit.digest(original), "scope": "upstream-archive", "categories": ["private-state-or-key-file"],
              "origin": "https://example.org/pinned-source.tar.gz", "reason": "Exact public debug test identity"}
    verifier = audit.Auditor({}, [review])
    verifier.inspect("source.zip", original)
    assert verifier.report()["passed"]
    changed = audit.Auditor({}, [review])
    changed.inspect("source.zip", zipped({key_name: b"different identity"}))
    assert not changed.report()["passed"]


def test_review_record_cannot_exempt_any_private_marker(tmp_path):
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps([{"sha256": "a" * 64, "origin": "https://example.org/", "reason": "fixture",
                                "categories": ["private-marker:owner"]}]))
    with pytest.raises(ValueError, match="cannot be waived"):
        audit.read_reviews(path)


def godot_bytes(identifier, compressed=False):
    encoded = identifier.encode("utf-32le")
    table = struct.pack("<IIII", 1, 0, 0, 0) + struct.pack("<I", len(identifier)) + bytes(v ^ 0xB6 for v in encoded)
    if compressed:
        zstandard = pytest.importorskip("zstandard")
        return b"GDSC" + struct.pack("<II", 101, len(table)) + zstandard.ZstdCompressor().compress(table)
    return b"GDSC" + struct.pack("<II", 101, 0) + table


@pytest.mark.parametrize("compressed", [False, True])
def test_compiled_godot_identifier_is_decompressed_and_unmasked(compressed):
    verifier = audit.Auditor({"owner": [OWNER]})
    verifier.inspect("menu.gdc", godot_bytes(OWNER, compressed))
    assert verifier.counts["godot_scripts_decoded"] == 1
    assert not verifier.report()["passed"]
    assert any(f["path"].endswith("::decoded-identifiers") for f in verifier.findings)
    assert not verifier.errors


def test_unsupported_or_truncated_godot_container_fails_closed():
    verifier = audit.Auditor({})
    verifier.inspect("menu.gdc", b"GDSC" + struct.pack("<II", 999, 0))
    assert not verifier.report()["passed"]
    assert verifier.errors[0]["category"] == "godot-inspection-failed"


def test_corrupt_archive_and_archive_depth_limit_fail_closed():
    verifier = audit.Auditor({})
    verifier.inspect("corrupt.apk", b"not a zip")
    assert not verifier.report()["passed"]
    nested = zipped({"one.zip": zipped({"two.txt": b"safe"})})
    limited = audit.Auditor({}, max_depth=1)
    limited.inspect("source.zip", nested)
    assert any(e["category"] == "archive-depth-limit" for e in limited.errors)


def test_directory_scan_includes_binary_and_excludes_only_git_object_storage(tmp_path):
    (tmp_path / "font.otf").write_bytes(b"OTTO" + OWNER.encode())
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/ignored-object").write_text(OWNER)
    result = audit.Auditor({"owner": [OWNER]}).audit([tmp_path])
    assert result["counts"]["payload_occurrences"] == 1
    assert not result["passed"]


def test_clean_native_and_nested_apk_passes_without_any_skip():
    verifier = audit.Auditor({"owner": [OWNER]})
    verifier.inspect("release.zip", zipped({"release.apk": zipped({"lib/native.so": b"\x7fELF\x00clean-library"})}))
    result = verifier.report()
    assert result["passed"]
    assert result["counts"]["archives_opened"] == 2
    assert not result["errors"] and not result["findings"]


def test_zstd_nested_source_archive_is_decoded_and_owner_is_found():
    zstandard = pytest.importorskip("zstandard")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        payload = OWNER.encode()
        member = tarfile.TarInfo("source/native.c")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    verifier = audit.Auditor({"owner": [OWNER]})
    compressed = zstandard.ZstdCompressor().compress(buffer.getvalue())
    verifier.inspect("sources.zip", zipped({"dependency.tar.zst": compressed}))
    assert verifier.counts["archives_opened"] == 2
    assert not verifier.report()["passed"] and not verifier.errors


def test_missing_godot_decoder_cannot_report_success(monkeypatch):
    import sys
    compressed = godot_bytes(OWNER, True)
    monkeypatch.setitem(sys.modules, "zstandard", None)
    verifier = audit.Auditor({"owner": [OWNER]})
    verifier.inspect("menu.gdc", compressed)
    assert not verifier.report()["passed"]
    assert verifier.errors[0]["error_type"] == "ModuleNotFoundError"


def test_private_filename_is_detected_even_when_its_content_is_clean(tmp_path):
    (tmp_path / (OWNER + ".otf")).write_bytes(b"OTTO\x00clean")
    verifier = audit.Auditor({"owner": [OWNER]})
    result = verifier.audit([tmp_path])
    assert not result["passed"]
    assert OWNER not in json.dumps(result)


def test_synthetic_source_review_is_explicitly_not_a_public_upstream_claim(tmp_path):
    source = ("/" + "home/demo-user/source/fixture").encode()
    review = {"sha256": audit.digest(source), "scope": "synthetic-fixture", "categories": ["generic-user-home-path"],
              "origin": "repository:tests/test_example.py", "reason": "Controlled synthetic fixture, no real owner values"}
    path = tmp_path / "review.json"
    path.write_text(json.dumps([review]))
    verifier = audit.Auditor({}, audit.read_reviews(path))
    verifier.inspect("tests/test_example.py", source)
    assert verifier.report()["passed"]
    assert verifier.findings[0]["status"] == "reviewed-synthetic-fixture"


def test_report_write_failure_never_prints_exception_private_values(tmp_path, monkeypatch, capsys):
    import sys
    source = tmp_path / "clean.so"
    source.write_bytes(b"\x7fELF\x00clean")
    monkeypatch.setattr(sys, "argv", ["privacy_audit.py", str(source), "--output", str(tmp_path / "report.json")])

    def fail_write(*args, **kwargs):
        raise OSError(OWNER)

    monkeypatch.setattr(Path, "write_text", fail_write)
    assert audit.main() == 2
    captured = capsys.readouterr()
    assert OWNER not in captured.out and OWNER not in captured.err
    assert json.loads(captured.out)["category"] == "audit-report-write-failed"


@pytest.mark.parametrize("before,after,expected", [("", "", True), (" ", "!", True), ("x", "", False), ("_", "", False), ("", "_", False)])
def test_fast_token_prefix_search_preserves_ascii_word_boundaries(before, after, expected):
    token = "hf" + "_" + "A" * 34
    verifier = audit.Auditor({})
    verifier.inspect("boundary.txt", (before + token + after).encode())
    assert any(f["category"] == "huggingface-token" for f in verifier.findings) is expected


def test_utf16_owner_without_any_nul_is_still_inspected():
    marker = "나다라마바"
    raw = marker.encode("utf-16le")
    assert b"\x00" not in raw
    verifier = audit.Auditor({"owner": [marker]})
    verifier.inspect("resource.bin", raw)
    assert not verifier.report()["passed"]
    assert verifier.findings[0]["encoding"] == "utf-16le"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16le", "utf-16be"])
def test_unicode_owner_case_variants_preserve_nonascii_code_units(encoding):
    marker = "한테스트User"
    verifier = audit.Auditor({"owner": [marker]})
    verifier.inspect("profile.bin", "한테스트uSeR".encode(encoding))
    assert not verifier.report()["passed"]
    assert verifier.findings[0]["encoding"] == encoding


@pytest.mark.parametrize("body", ["A" * 31 + "-", "A" * 32 + "-", "-" * 32, "_" * 32, "A" * 33])
@pytest.mark.parametrize("enclosing_bad_word", [False, True])
def test_fast_token_search_matches_original_regex_for_trailing_dash_and_nested_tokens(body, enclosing_bad_word):
    import re
    category = "openai-token"
    token = "sk" + "-" + body
    text = ("x" if enclosing_bad_word else " ") + token + "-" + "sk" + "-" + "B" * 34
    raw = text.encode()
    expected = list(re.finditer(audit.PATTERNS[category], raw))
    found = [f for f in audit.Auditor({}).scan_bytes(raw) if f["category"] == category]
    assert sum(f["count"] for f in found) == len(expected)
    assert [offset for f in found for offset in f["offsets"]] == [m.start() for m in expected]


def test_exact_original_uncompressed_archive_review_covers_container_and_fixture_bytes():
    begin = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5
    end = "-" * 5 + "END RSA PRIVATE KEY" + "-" * 5
    key = (begin + "\n" + "ABCD" * 64 + "\n" + end).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("tests/example-key.pem")
        member.size = len(key)
        archive.addfile(member, io.BytesIO(key))
    raw = output.getvalue()
    review = {"sha256": audit.digest(raw), "scope": "upstream-archive", "categories": ["private-key-block"],
              "origin": "https://example.org/pinned-original.tar", "reason": "Exact original public test fixture archive"}
    verifier = audit.Auditor({}, [review])
    verifier.inspect("original.tar", raw)
    assert verifier.report()["passed"]
    assert len(verifier.findings) == 2
    assert all(f["status"] == "reviewed-public-origin" for f in verifier.findings)


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_original_archive_authority_never_waives_owner_in_container_or_child(kind):
    body = OWNER.encode()
    if kind == "zip":
        raw = zipped({"upstream.txt": body})
    else:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            member = tarfile.TarInfo("upstream.txt")
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
        raw = output.getvalue()
    review = {"sha256": audit.digest(raw), "scope": "upstream-archive",
              "categories": ["private-marker:owner"],
              "origin": "https://example.org/original-source", "reason": "Deliberately invalid owner waiver"}
    verifier = audit.Auditor({"owner": [OWNER]}, [review])
    verifier.inspect("original." + kind, raw)
    findings = [f for f in verifier.findings if f["category"] == "private-marker:owner"]
    assert findings
    assert any("!" in f["path"] for f in findings)
    if kind == "tar":
        assert any("!" not in f["path"] for f in findings)
    assert all(f["status"] == "blocked" for f in findings)
    assert not verifier.report()["passed"]
