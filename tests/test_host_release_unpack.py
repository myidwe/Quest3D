"""Public unpacker verifies archives before executing any extraction/build."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile

import pytest

spec = importlib.util.spec_from_file_location("unpack_host_release", Path(__file__).parents[1] /
                                            "scripts/release/unpack_host_release.py")
unpack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unpack)


def archive(path, names):
    with tarfile.open(path,'w:gz') as t:
        for name,data,kind in names:
            e=tarfile.TarInfo(name);e.size=len(data)
            if kind=='symlink':e.type=tarfile.SYMTYPE;e.linkname='../outside';e.size=0
            t.addfile(e,io.BytesIO(data) if e.isfile() else None)


@pytest.mark.parametrize('name',['/absolute','../escape','a/../../b','C:/root','x\\y','NUL.txt','a\nlog'])
def test_unsafe_member_rejected(name):
    with pytest.raises(ValueError):unpack.safe(name)


def test_changed_input_rejected_before_extraction(tmp_path):
    a=tmp_path/'source.tar.gz';destination=tmp_path/'new'
    archive(a,[('actual.cpp',b'changed','file')])
    with pytest.raises(ValueError,match='inventory'):
        unpack.extract(a,destination,{'actual.cpp':hashlib.sha256(b'original').hexdigest()})
    assert not destination.exists()


def test_bound_bytes_extract_without_build(tmp_path):
    a=tmp_path/'source.tar.gz';destination=tmp_path/'new'
    archive(a,[('src/actual.cpp',b'exact\r\n','file')])
    unpack.extract(a,destination,{'src/actual.cpp':hashlib.sha256(b'exact\r\n').hexdigest()})
    assert (destination/'src/actual.cpp').read_bytes()==b'exact\r\n'


@pytest.mark.parametrize('names',[[('A.cpp',b'x','file'),('a.cpp',b'y','file')],
                                 [('link',b'','symlink')]])
def test_alias_or_link_refused_before_extraction(tmp_path,names):
    a=tmp_path/'source.tar.gz';archive(a,names);destination=tmp_path/'new'
    with pytest.raises(ValueError,match='nonregular'):
        unpack.extract(a,destination)
    assert not destination.exists()
