"""A moved installation must not consume developer paths or unverified code."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quest3d.assets import verified_model_source
from quest3d.desktop_setup import distribution_layout
from quest3d.desktop_pairing import approve_pin


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def layout(root):
    return distribution_layout(root, host_sha='a'*64, runtime='artifacts/host/runtime-dev', capture='old/capture')


def test_clean_install_uses_its_own_relative_runtime_and_preserves_dev_default(tmp_path):
    assert layout(tmp_path)['distributed'] is False
    write(tmp_path/'config/distribution.json', {'schema': 1, 'host_sha256': 'a'*64,
          'host_runtime': 'artifacts/host/runtime-public', 'hdr_package': 'runtime/capture'})
    value = layout(tmp_path)
    assert value['distributed']
    assert value['runtime'] == tmp_path/'artifacts/host/runtime-public'
    assert value['capture'] == tmp_path/'runtime/capture'


@pytest.mark.parametrize('field,value', [('host_runtime', '../escape'), ('host_runtime', 'runtime/host'),
    ('hdr_package', 'C:/developer/capture'), ('hdr_package', '/capture'), ('host_sha256', 'b'*64), ('schema', 2)])
def test_install_metadata_refuses_escape_and_unrecognized_host(tmp_path, field, value):
    data = {'schema': 1, 'host_sha256': 'a'*64, 'host_runtime': 'artifacts/host/runtime-public', 'hdr_package': 'runtime/capture'}
    data[field] = value
    write(tmp_path/'config/distribution.json', data)
    with pytest.raises(ValueError):
        layout(tmp_path)


def prepare_model(root):
    source = root/'third_party/depth-anything-v2'
    file = source/'depth_anything_v2/dpt.py'
    file.parent.mkdir(parents=True)
    file.write_bytes(b'# fixed source\n')
    write(root/'config/models.json', {'depth_anything_v2_small': {'source_commit': 'source-pin'}})
    write(root/'config/depth-source.json', {'source_commit': 'source-pin',
          'files': {'depth_anything_v2/dpt.py': hashlib.sha256(file.read_bytes()).hexdigest()}})
    return source, file


def test_packaged_model_source_is_verified_without_git(tmp_path, monkeypatch):
    source, _ = prepare_model(tmp_path)
    monkeypatch.setattr('subprocess.check_output', lambda *a, **kw: pytest.fail('Must not need Git'))
    assert verified_model_source(tmp_path) == source


@pytest.mark.parametrize('failure', ['modified', 'extra', 'missing', 'wrong_revision'])
def test_packaged_model_source_tamper_fails_before_import(tmp_path, failure):
    source, file = prepare_model(tmp_path)
    if failure == 'modified': file.write_bytes(b'modified')
    elif failure == 'extra': (file.parent/'extra.py').write_text('pass')
    elif failure == 'missing': file.unlink()
    else:
        manifest = json.loads((tmp_path/'config/depth-source.json').read_text())
        manifest['source_commit'] = 'another'
        write(tmp_path/'config/depth-source.json', manifest)
    with pytest.raises(ValueError): verified_model_source(tmp_path)


def test_pairing_passes_pin_on_stdin_once_and_binds_fresh_request(tmp_path):
    calls = []
    def run(argv, **options):
        calls.append((argv, options))
        value = {'eligible_requests': [{'id': 'a'*32, 'address': '192.168.2.9'}]} if len(calls) == 1 else {'host_accepted': True}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')
    assert approve_pin(tmp_path, Path('pwsh.exe'), {}, '1234', run=run)['host_accepted']
    assert len(calls) == 2 and '1234' not in ' '.join(calls[1][0])
    assert json.loads(calls[1][1]['input']) == {'pin': '1234'}
    assert calls[1][0][-3:] == ['-PairingId', 'a'*32, '-PinFromStdin']


@pytest.mark.parametrize('requests', [[], [{'id': 'a'*32}, {'id': 'b'*32}]])
def test_pairing_does_not_submit_ambiguous_or_missing_request(tmp_path, requests):
    calls = []
    def run(argv, **options):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'eligible_requests': requests}))
    with pytest.raises(RuntimeError): approve_pin(tmp_path, Path('pwsh.exe'), {}, '1234', run=run)
    assert len(calls) == 1


def test_pairing_unknown_result_never_retries_or_leaks_stdin(tmp_path):
    calls = []
    def run(argv, **options):
        calls.append(argv)
        if len(calls) == 1: return SimpleNamespace(returncode=0, stdout=json.dumps({'eligible_requests': [{'id': 'a'*32}]}))
        raise ValueError('secret pin 1234 response body')
    with pytest.raises(RuntimeError) as error:
        approve_pin(tmp_path, Path('pwsh.exe'), {}, '1234', run=run)
    assert '1234' not in str(error.value) and len(calls) == 2
