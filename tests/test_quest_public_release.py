"""Actual public-release boundary failures, without keystore/device writes."""
import ctypes
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import zipfile

import pytest

RELEASE=Path(__file__).resolve().parents[1]/'scripts/release'
sys.path.insert(0,str(RELEASE))
vendor=importlib.import_module('collect_quest_vendor_baseline')
deps=importlib.import_module('collect_quest_release_dependencies')
identity=importlib.import_module('windows_release_identity')
source_supply=importlib.import_module('complete_quest_source_supply')
signing=importlib.import_module('sign_quest_release')
sys.path.remove(str(RELEASE))

@pytest.mark.parametrize('name,kind',[('root/../../escape','file'),('root/link','symlink'),('root/hard','hardlink'),('root-other/file','file')])
def test_vendor_archive_rejects_traversal_and_links(tmp_path,name,kind):
    archive=tmp_path/'input.tar'
    with tarfile.open(archive,'w') as target:
        item=tarfile.TarInfo(name)
        if kind=='symlink':item.type=tarfile.SYMTYPE;item.linkname='../../outside'
        elif kind=='hardlink':item.type=tarfile.LNKTYPE;item.linkname='root/source'
        else:item.size=4
        target.addfile(item,io.BytesIO(b'data') if kind=='file' else None)
    with pytest.raises(ValueError):vendor.extract(archive,'root',tmp_path/'target')
    assert not (tmp_path/'escape').exists()

def test_existing_vendor_output_is_preserved(tmp_path):
    existing=tmp_path/'existing';existing.mkdir()
    protected=existing/'owned';protected.write_bytes(b'keep')
    with pytest.raises(FileExistsError):vendor.prepare(tmp_path/'nonexistent-review',existing)
    assert protected.read_bytes()==b'keep'

def test_nested_runtime_notice_is_retained():
    inner=io.BytesIO()
    with zipfile.ZipFile(inner,'w') as z:z.writestr('META-INF/NOTICE','Original copyright notice')
    outer=io.BytesIO()
    with zipfile.ZipFile(outer,'w') as z:
        z.writestr('classes.jar',inner.getvalue());z.writestr('META-INF/LICENSE','Original license')
    assert deps.notices(outer.getvalue())=={'classes.jar/META-INF/NOTICE':b'Original copyright notice','META-INF/LICENSE':b'Original license'}

def test_unbounded_license_entry_rejected():
    source=io.BytesIO()
    with zipfile.ZipFile(source,'w',compression=zipfile.ZIP_DEFLATED) as z:z.writestr('NOTICE',b'x'*(2*1024*1024+1))
    with pytest.raises(ValueError):deps.notices(source.getvalue())

def test_private_identity_rejects_checkout_directory_without_write(tmp_path):
    workspace=tmp_path/'repo';workspace.mkdir()
    with pytest.raises(ValueError):identity.create(workspace/'private','unused',workspace)
    assert not (workspace/'private').exists()

def test_existing_private_identity_is_preserved(tmp_path):
    existing=tmp_path/'private';existing.mkdir();(existing/'key.p12').write_bytes(b'preserve')
    with pytest.raises(FileExistsError):identity.create(existing,'unused',tmp_path/'repo')
    assert (existing/'key.p12').read_bytes()==b'preserve'

@pytest.mark.skipif(os.name!='nt',reason='Actual current-user Windows DPAPI boundary')
def test_actual_current_user_dpapi_roundtrip():
    sample=b'non-secret DPAPI self-test fixture'
    encrypted=identity.dpapi(sample)
    assert encrypted!=sample
    assert identity.dpapi(encrypted,True)==sample

def test_signing_wrapper_keeps_password_out_of_arguments_and_report(tmp_path,monkeypatch):
    private=tmp_path/'private';private.mkdir()
    certificate='a'*64
    (private/'public-identity.json').write_text(json.dumps({'certificate_sha256':certificate,'alias':'test-release'}))
    (private/'password.dpapi').write_bytes(b'encrypted fixture')
    spec=tmp_path/'spec.json';spec.write_text(json.dumps({'apk_metadata':{'certificate_sha256':certificate}}))
    password=b'private-test-password'
    monkeypatch.setattr(identity,'dpapi',lambda value,decrypt:password)
    monkeypatch.setattr(identity,'wsl_path',lambda value:'/mnt/test/'+value.name)
    seen={}
    def run(argv,**kwargs):
        seen.update(argv=argv,kwargs=kwargs)
        result={'signed':True,'apk_metadata':{'certificate_sha256':certificate}}
        return type('Result',(),{'returncode':0,'stdout':json.dumps(result).encode(),'stderr':b'private diagnostics'})()
    monkeypatch.setattr(identity.subprocess,'run',run)
    report=identity.sign_public(private,tmp_path/'repo','/pinned-cache',spec,tmp_path/'input.apk',tmp_path/'source',tmp_path/'output')
    assert seen['kwargs']['input']==password+b'\n'+password+b'\n'
    assert not any(password.decode() in argument for argument in seen['argv'])
    assert 'env' not in seen['kwargs'] and password.decode() not in json.dumps(report)
    assert '/pinned-cache/android-sdk/build-tools/36.1.0/aapt2' in seen['argv']
    assert '--passwords-from-stdin' in seen['argv']

def test_signing_wrapper_rejects_wrong_certificate_before_private_recovery(tmp_path,monkeypatch):
    private=tmp_path/'private';private.mkdir()
    (private/'public-identity.json').write_text(json.dumps({'certificate_sha256':'a'*64,'alias':'test-release'}))
    spec=tmp_path/'spec.json';spec.write_text(json.dumps({'apk_metadata':{'certificate_sha256':'b'*64}}))
    def forbidden(*args,**kwargs):raise AssertionError('Private recovery must not run')
    monkeypatch.setattr(identity,'dpapi',forbidden)
    with pytest.raises(ValueError):identity.sign_public(private,tmp_path/'repo','/cache',spec,tmp_path/'input.apk',tmp_path/'source',tmp_path/'output')

def test_retired_sdk_header_in_source_archive_is_rejected(tmp_path):
    archive=tmp_path/'source.tar'
    with tarfile.open(archive,'w') as target:
        item=tarfile.TarInfo('source/meta_headers/openxr_preview.h');item.size=6
        target.addfile(item,io.BytesIO(b'header'))
    with pytest.raises(ValueError):source_supply.public_header_audit(tmp_path)
    assert not (tmp_path/'PUBLIC_HEADER_INPUT_AUDIT.json').exists()

def test_public_header_source_and_legacy_url_are_distinguished(tmp_path):
    legacy=tmp_path/'project/addons/godotopenxrvendors/meta/LICENSE-SDK'
    legacy.parent.mkdir(parents=True);legacy.write_text('Historical SDK license URL')
    archive=tmp_path/'source.tar'
    with tarfile.open(archive,'w') as target:
        item=tarfile.TarInfo('source/include/openxr/openxr.h');item.size=6
        target.addfile(item,io.BytesIO(b'header'))
    report=source_supply.public_header_audit(tmp_path)
    assert report['proprietary_sdk_path_matches']==[]
    assert report['legacy_sdk_license_reference']['sha256']==source_supply.sha(legacy)
    assert report['archive_inventory'][0]['sha256']==source_supply.sha(archive)

def test_explicit_password_pipe_uses_stdin_instead_of_wsl_terminal(monkeypatch):
    monkeypatch.setattr(signing.sys,'stdin',io.StringIO('private fixture\n\n'))
    monkeypatch.setattr(signing.getpass,'getpass',lambda prompt:pytest.fail('Must not open /dev/tty in pipe mode'))
    assert signing.passwords(True)==('private fixture','private fixture')

@pytest.mark.parametrize('value',['missing newline','first\n','first\nsecond\x00\n'])
def test_password_pipe_rejects_incomplete_or_invalid_input(monkeypatch,value):
    monkeypatch.setattr(signing.sys,'stdin',io.StringIO(value))
    with pytest.raises(ValueError):signing.passwords(True)
