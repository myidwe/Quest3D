"""Historical source assembly refuses unsafe archives before creating a tree."""
import importlib.util
import io
from pathlib import Path
import tarfile

import pytest

spec = importlib.util.spec_from_file_location(
    "host_source_preparation", Path(__file__).parents[1] / "scripts/release/prepare_host_source.py")
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)


def archive_entries(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                data = b"public source"
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                info.linkname = "../outside"
                archive.addfile(info)
    return output.getvalue()


@pytest.mark.parametrize("name", ["../outside.py", "/absolute.py", "C:/source.py", "dir/CON.txt"])
def test_archive_escape_rejected_before_creating_destination(tmp_path, name):
    destination = tmp_path / "source"
    with pytest.raises(ValueError):
        preparation.extract_archive(archive_entries([(name, tarfile.REGTYPE)]), destination)
    assert not destination.exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_links_and_special_files_are_not_extracted(tmp_path, kind):
    destination = tmp_path / "source"
    with pytest.raises(ValueError):
        preparation.extract_archive(archive_entries([("link", kind)]), destination)
    assert not destination.exists()


def test_case_alias_is_rejected_before_extraction(tmp_path):
    with pytest.raises(ValueError):
        preparation.extract_archive(archive_entries([
            ("src/File.cpp", tarfile.REGTYPE), ("src/file.cpp", tarfile.REGTYPE)]), tmp_path / "source")
    assert not (tmp_path / "source").exists()


def test_regular_archive_can_be_assembled_without_git_metadata(tmp_path):
    destination = tmp_path / "source"
    preparation.extract_archive(archive_entries([("src/file.cpp", tarfile.REGTYPE)]), destination)
    assert (destination / "src/file.cpp").read_bytes() == b"public source"
    assert len(preparation.hashes(destination)) == 1
    assert not (destination / ".git").exists()


def test_missing_source_root_is_not_a_valid_empty_inventory(tmp_path):
    with pytest.raises(FileNotFoundError):
        preparation.hashes(tmp_path / "absent-source")


def test_regular_file_is_not_a_valid_source_directory(tmp_path):
    source = tmp_path / "file.cpp"
    source.write_text("public source", "utf-8")
    with pytest.raises(NotADirectoryError):
        preparation.hashes(source)
