"""격리 후보 patch는 예상 API 한 곳만 변경하고 source 혼합을 거부한다."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "host_compat_candidate", Path(__file__).parents[1] / "scripts/release/prepare_host_compat_candidate.py")
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)


def test_patch_changes_only_packet_access_and_keeps_surrounding_production_code():
    surrounding = b"// before\n" + candidate.OLD + b"      auto session = (session_t *) channel_data;\n// after\n"
    patched = candidate.compatibility_patch(surrounding)
    assert patched.replace(candidate.NEW, candidate.OLD) == surrounding
    assert patched.count(b"packet->first") == 1
    assert patched.count(b"packet->second") == 1


def test_patch_preserves_crlf_without_rewriting_the_rest_of_the_file():
    old = candidate.OLD.replace(b"\n", b"\r\n")
    new = candidate.NEW.replace(b"\n", b"\r\n")
    source = b"// before\r\n" + old + b"// after\r\n"
    patched = candidate.compatibility_patch(source)
    assert patched.replace(new, old) == source
    assert patched.count(b"\r\n") == source.count(b"\r\n") + 1


@pytest.mark.parametrize("source", [b"unrelated source", candidate.OLD * 2, candidate.NEW])
def test_patch_refuses_missing_duplicate_or_already_modified_site(source):
    with pytest.raises(ValueError):
        candidate.compatibility_patch(source)


def test_original_directory_is_not_a_candidate_destination(tmp_path):
    historical = tmp_path / "historical"
    historical.mkdir()
    with pytest.raises(FileExistsError):
        candidate.prepare(historical, historical / "candidate")
    assert not (historical / "candidate").exists()


def test_existing_candidate_is_never_overwritten(tmp_path):
    output = tmp_path / "candidate"
    output.mkdir()
    marker = output / "preserved.txt"
    marker.write_text("keep", "utf-8")
    with pytest.raises(FileExistsError):
        candidate.prepare(tmp_path / "historical", output)
    assert marker.read_text("utf-8") == "keep"


def test_finalize_refuses_a_directory_with_previous_outputs(tmp_path):
    output = tmp_path / "candidate"
    output.mkdir()
    (output / "source").mkdir()
    marker = output / "candidate-provenance.json"
    marker.write_text("preserve", "utf-8")
    with pytest.raises(FileExistsError):
        candidate.prepare(tmp_path / "historical", output, finalize_only=True)
    assert marker.read_text("utf-8") == "preserve"
