"""New binary supply retains exact source bytes and refuses aliases/overwrites."""
import hashlib
import importlib.util
from pathlib import Path
import tarfile

import pytest

spec = importlib.util.spec_from_file_location("host_release_inputs", Path(__file__).parents[1] /
                                            "scripts/release/prepare_host_release_inputs.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize("name", ["../escape", "/abs", "a\\b", "C:/x", "a/NUL.txt",
                                   "x. ", "a//b", "a\nsecret"])
def test_unsafe_supply_name_refused(name):
    with pytest.raises(ValueError):
        release.relative_name(name)


def test_source_archive_contains_only_hash_bound_build_inputs(tmp_path):
    source=tmp_path/'source';source.mkdir()
    (source/'actual.cpp').write_bytes(b'actual code\r\n')
    (source/'build.log').write_bytes(b'private local build information')
    (source/'node_modules').mkdir();(source/'node_modules/ignore.js').write_text('build dependency')
    expected={'actual.cpp':hashlib.sha256(b'actual code\r\n').hexdigest()}
    output=tmp_path/'source.tar.gz'
    report=release.source_archive(source,expected,output)
    assert report['every_member_sha_verified'] is True
    assert report['source_files']==expected
    with tarfile.open(output) as archive:
        assert archive.getnames()==['actual.cpp']
        assert archive.extractfile('actual.cpp').read()==b'actual code\r\n'
    assert (source/'build.log').read_bytes()==b'private local build information'


def test_changed_source_refused_before_archive_creation(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'x.cpp').write_bytes(b'changed')
    output=tmp_path/'source.tar.gz'
    with pytest.raises(ValueError,match='authority'):
        release.source_archive(source,{'x.cpp':hashlib.sha256(b'original').hexdigest()},output)
    assert not output.exists()


def test_case_aliased_source_identity_refused(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'X.cpp').write_bytes(b'x')
    with pytest.raises(ValueError,match='Aliased'):
        release.source_archive(source,{'X.cpp':hashlib.sha256(b'x').hexdigest(),
                                     'x.cpp':hashlib.sha256(b'x').hexdigest()},tmp_path/'source.tar.gz')


def test_copy_preserves_old_files_and_hash_bound_input(tmp_path):
    original=tmp_path/'input';original.write_bytes(b'reviewed')
    expected=release.sha(original)
    destination=tmp_path/'output'
    release.put_file(destination,original,expected)
    assert destination.read_bytes()==original.read_bytes()==b'reviewed'
    with pytest.raises(FileExistsError):
        release.put_file(destination,original,expected)
    with pytest.raises(ValueError,match='authority'):
        release.put_file(tmp_path/'wrong',original,'0'*64)
