"""Embedded installer content must meet the same privacy gate as plain ZIPs."""
import importlib.util
import io
import json
from pathlib import Path
import struct
import zipfile

import pytest

ROOT = Path(__file__).parents[1]


def release_module(name):
    spec = importlib.util.spec_from_file_location("exe_privacy_test_" + name, ROOT / "scripts/release" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = release_module("privacy_audit")
payload_format = release_module("exe_payload")
OWNER = "private-fixture-exe-owner"


def zipped(files, comment=b""):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.comment = comment
    return output.getvalue()


def setup(files=None, *, payload=None, extra_stub=b""):
    if payload is None:
        payload = zipped(files if files is not None else {"readme.txt": b"safe"})
    stub = b"MZ" + b"\0" * 62 + payload_format.SETUP_MARKER.encode("utf-16le") + extra_stub
    return stub + payload + payload_format.pack_footer(len(stub), payload)


def verifier(data, *, name="Quest3D-Setup.exe", reviews=None, **limits):
    result = audit.Auditor({"owner": [OWNER]}, reviews, **limits)
    result.inspect(name, data)
    return result


def godot_identifier(identifier, compressed):
    encoded = identifier.encode("utf-32le")
    table = struct.pack("<IIII", 1, 0, 0, 0) + struct.pack("<I", len(identifier)) + bytes(v ^ 0xB6 for v in encoded)
    if compressed:
        import zstandard
        return b"GDSC" + struct.pack("<II", 101, len(table)) + zstandard.ZstdCompressor().compress(table)
    return b"GDSC" + struct.pack("<II", 101, 0) + table


def test_valid_setup_scans_raw_stub_and_verified_embedded_zip():
    data = setup()
    result = verifier(data)
    assert result.report()["passed"]
    assert result.counts["setup_exe_payloads_decoded"] == 1
    assert result.counts["archives_opened"] == 1
    assert result.counts["payload_occurrences"] == 3
    assert result.counts["setup_exe_stub_bytes_inspected"] > 2
    assert result.counts["setup_exe_payload_bytes_inspected"] > 4


def test_compressed_token_is_found_inside_embedded_zip():
    token = ("gh" + "p_" + "K" * 36).encode()
    data = setup({"configuration.txt": token})
    assert token not in data
    result = verifier(data)
    finding = next(f for f in result.findings if f["category"] == "github-token")
    assert finding["path"].endswith("!embedded-payload.zip!configuration.txt")
    assert finding["status"] == "blocked"
    assert not result.report()["passed"]
    assert token.decode() not in json.dumps(result.report())


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16le", "utf-16be"])
@pytest.mark.parametrize("location", ["stub", "payload"])
def test_owner_in_stub_or_embedded_file_is_blocked_in_every_encoding(encoding, location):
    owner = OWNER.upper().encode(encoding)
    data = setup(extra_stub=owner) if location == "stub" else setup({"media.bin": owner})
    result = verifier(data)
    assert not result.report()["private_marker_gate_passed"]
    assert any(f["category"] == "private-marker:owner" and f["encoding"] == encoding for f in result.findings)
    assert OWNER not in json.dumps(result.report())


@pytest.mark.parametrize("compressed", [False, True])
def test_embedded_compiled_godot_identifiers_are_decoded(compressed):
    data = setup({"menu.gdc": godot_identifier(OWNER, compressed)})
    assert OWNER.encode() not in data
    result = verifier(data)
    assert result.counts["godot_scripts_decoded"] == 1
    assert not result.errors
    assert not result.report()["passed"]
    assert any(f["path"].endswith("::decoded-identifiers") and f["category"] == "private-marker:owner" for f in result.findings)


def test_embedded_nested_zip_names_and_comments_are_privacy_data():
    nested = zipped({OWNER + ".txt": b"safe"}, comment=OWNER.encode())
    result = verifier(setup({"nested.zip": nested}))
    assert result.counts["archives_opened"] == 2
    assert not result.report()["passed"]
    assert any(f["path"].endswith("::archive-metadata") for f in result.findings)
    assert OWNER not in json.dumps(result.report())


def test_public_nested_archive_review_still_cannot_waive_owner():
    generic_home = ("/" + "home/publisher/source/").encode()
    nested = zipped({"example.txt": generic_home + OWNER.encode()})
    review = {"sha256": audit.digest(nested), "scope": "upstream-archive",
              "categories": ["generic-user-home-path", "private-marker:owner"],
              "origin": "https://example.org/pinned-original.zip", "reason": "Deliberately invalid owner waiver"}
    result = verifier(setup({"original.zip": nested}), reviews=[review])
    owner = next(f for f in result.findings if f["category"] == "private-marker:owner")
    home = next(f for f in result.findings if f["category"] == "generic-user-home-path")
    assert owner["status"] == "blocked"
    assert home["status"] == "reviewed-public-origin"
    assert not result.report()["passed"]


def test_ordinary_upstream_pe_is_not_required_to_have_a_setup_footer(monkeypatch):
    def unavailable():
        raise ImportError("Unavailable setup decoder")
    monkeypatch.setattr(audit, "setup_payload_module", unavailable)
    result = verifier(b"MZ" + b"ordinary published Windows utility", name="vendor.exe")
    assert result.report()["passed"]
    assert not result.errors
    assert not result.counts["setup_exe_payloads_decoded"]


def test_renaming_setup_does_not_hide_embedded_content():
    result = verifier(setup({"configuration.txt": OWNER.encode()}), name="payload.bin")
    assert result.counts["setup_exe_payloads_decoded"] == 1
    assert not result.report()["passed"]


@pytest.mark.parametrize("damage", ["tail-truncated", "footer-removed", "magic-version", "payload-byte", "hash-byte", "size", "stub-offset", "missing-mz", "trailing-byte"])
def test_claimed_setup_with_corrupt_footer_or_integrity_fails_closed(damage):
    data = setup()
    magic, offset, length, expected = payload_format.FOOTER.unpack(data[-payload_format.FOOTER_SIZE:])
    if damage == "tail-truncated":
        data = data[:-1]
    elif damage == "footer-removed":
        data = data[:-payload_format.FOOTER_SIZE]
    elif damage == "magic-version":
        data = data[:-payload_format.FOOTER_SIZE] + payload_format.FOOTER.pack(b"Q3DSETUPZIPv2".ljust(16, b"\0"), offset, length, expected)
    elif damage == "payload-byte":
        data = data[:offset + 10] + bytes([data[offset + 10] ^ 1]) + data[offset + 11:]
    elif damage == "hash-byte":
        data = data[:-1] + bytes([data[-1] ^ 1])
    elif damage == "size":
        data = data[:-payload_format.FOOTER_SIZE] + payload_format.FOOTER.pack(magic, offset, length + 1, expected)
    elif damage == "stub-offset":
        data = data[:-payload_format.FOOTER_SIZE] + payload_format.FOOTER.pack(magic, offset + 1, length - 1, expected)
    elif damage == "missing-mz":
        data = b"XX" + data[2:]
    elif damage == "trailing-byte":
        data += b"x"
    result = verifier(data)
    assert not result.report()["passed"]
    assert result.errors[0]["category"] == "setup-exe-inspection-failed"


@pytest.mark.parametrize("encoding", ["ascii", "utf-16le", "utf-16be"])
def test_unknown_bootstrap_version_claim_fails_closed(encoding):
    result = verifier(b"MZ" + "Q3D_SETUP_BOOTSTRAP_V999".encode(encoding))
    assert not result.report()["passed"]
    assert result.errors[0]["category"] == "setup-exe-inspection-failed"


def test_corrupt_zip_with_valid_footer_hash_still_fails_closed():
    result = verifier(setup(payload=b"PK\x03\x04not-an-archive"))
    assert not result.report()["passed"]
    assert any(error["category"] == "archive-inspection-failed" for error in result.errors)


def test_missing_setup_decoder_fails_closed_and_does_not_emit_exception_identity(monkeypatch):
    def unavailable():
        raise ImportError("Missing " + OWNER)
    monkeypatch.setattr(audit, "setup_payload_module", unavailable)
    result = verifier(setup())
    assert not result.report()["passed"]
    assert result.errors[0]["category"] == "setup-exe-inspection-failed"
    assert result.errors[0]["error_type"] == "ImportError"
    assert OWNER not in json.dumps(result.report())


def test_embedded_setup_cannot_bypass_archive_depth_limit():
    result = verifier(setup(), max_depth=0)
    assert not result.report()["passed"]
    assert result.errors[0]["category"] == "setup-exe-depth-limit"


def test_exe_really_reads_embedded_data_for_actual_file_input(tmp_path):
    path = tmp_path / "Quest3D-Setup.exe"
    path.write_bytes(setup({"capture-state.txt": OWNER.encode("utf-16le")}))
    result = audit.Auditor({"owner": [OWNER]}).audit([path])
    assert not result["passed"]
    assert result["inputs"][0]["sha256"] == audit.digest(path.read_bytes())
    assert result["counts"]["setup_exe_payloads_decoded"] == 1
