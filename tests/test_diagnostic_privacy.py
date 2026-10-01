"""Synthetic sharing cases; no actual credentials, logs or private files are read."""
import copy
import json

import pytest

from quest3d.diagnostic_privacy import DiagnosticRedactor, REDACTED


def pem_marker(direction, kind='PRIVATE KEY'):
    # Assemble synthetic fixture labels so source-export secret scanners do not
    # confuse test source with a real embedded key. Runtime test bytes are exact.
    return '-----' + direction + ' ' + kind + '-----'


def test_shared_copy_removes_nested_identity_secret_and_launch_arguments():
    original = {
        'preferences': {'mode': '3d', 'depth_percent': 1.3125, 'monitor_device': 'fixture-monitor'},
        'state': {'pc_address': '192.0.2.10', 'connected': True, 'published_frames': 720,
                  'audio_outputs': [{'device_id': 'fixture-device', 'friendly_name': 'Fixture headphones'}]},
        'producer': {'pid': 101, 'exe': 'D:\\Fixture User\\python.exe'},
        'details': {'api_key': {'nested': 'fixture-secret-value'}, 'pairing_id': 'fixture-pairing',
                    'arguments': ['sunshine', '--creds', 'fixture-user', 'fixture-password']},
        'media': {'path': '/home/fixture-user/private/holiday.mp4'},
    }
    retained = copy.deepcopy(original)
    redactor = DiagnosticRedactor()
    shared = redactor.value(original)
    assert original == retained
    serialized = json.dumps(shared)
    for private in ['192.0.2.10', 'fixture-monitor', 'Fixture headphones', 'fixture-device',
                    'Fixture User', 'fixture-secret-value', 'fixture-password', 'holiday.mp4', 'fixture-pairing']:
        assert private not in serialized
    assert shared['state']['published_frames'] == 720
    assert shared['state']['connected'] is True
    assert shared['preferences']['depth_percent'] == 1.3125
    assert shared['preferences']['mode'] == '3d'
    assert shared['producer']['pid'] == 101
    assert shared['details']['api_key'] == REDACTED
    assert redactor.redactions > 0


@pytest.mark.parametrize('private', [
    '192.0.2.10', '2001:db8::12', 'fe80::1%fixture', 'AA:BB:CC:DD:EE:FF',
    'fixture.person@example.test', 'D:\\Fixture User\\private video.mp4',
    '/home/fixture-user/private/holiday.mp4', '/mnt/d/private/source.py',
    'https://fixture-user:fixture-password@host.example.test/connect?token=fixture-secret',
    'adb -s FIXTURE123456 install app.apk',
    'a1192394-81d0-4b4a-9b4c-3b66c6440c66',
    'qA12bC34dE56fG78hJ90kL12mN34pQ56rS78',
])
def test_free_text_identity_paths_and_opaque_values_are_removed(private):
    text = 'Begin: ' + private + '\nframes=720 fps=59.4\n'
    result = DiagnosticRedactor().text(text)
    assert private not in result
    assert REDACTED in result
    assert 'frames=720 fps=59.4' in result


@pytest.mark.parametrize('line', [
    'password = fixture-secret-value', 'Authorization: Bearer fixture-secret-value',
    '--token fixture-secret-value', 'sunshine --creds fixture-user fixture-secret-value',
    'pin: 1234', 'serial = FIXTURE123456', 'device_id: fixture-device',
])
def test_plaintext_credentials_are_not_kept(line):
    result = DiagnosticRedactor().text(line + '\nFPS=58.2')
    assert 'fixture-secret-value' not in result
    assert '1234' not in result
    assert 'FIXTURE123456' not in result
    assert 'fixture-device' not in result
    assert 'FPS=58.2' in result


def test_jsonl_is_parsed_before_sanitizing_escaped_paths_and_integer_pin():
    value = {'kind': 'setup_error', 'token': 'fixture-secret', 'pin': 1234,
             'path': 'D:\\Fixture User\\secret video.mp4', 'frames': 400}
    raw = json.dumps(value) + '\n' + json.dumps({'fps': 31.5}) + '\n'
    shared = DiagnosticRedactor().log(raw)
    lines = [json.loads(line) for line in shared.splitlines()]
    assert lines[0]['path'] == REDACTED
    assert lines[0]['token'] == REDACTED
    assert lines[0]['pin'] == REDACTED
    assert lines[0]['frames'] == 400 and lines[1]['fps'] == 31.5
    for private in ('Fixture User', 'secret video', 'fixture-secret', '1234'):
        assert private not in shared


@pytest.mark.parametrize('pem', [
    pem_marker('BEGIN') + '\nfixture-key-data\n' + pem_marker('END'),
    pem_marker('BEGIN', 'EC PRIVATE KEY') + '\nfixture-key-data\n',
    'fixture-key-data\n' + pem_marker('END'),
])
def test_complete_and_tail_truncated_private_key_blocks_are_removed(pem):
    shared = DiagnosticRedactor().log(pem)
    assert 'fixture-key-data' not in shared
    assert 'PRIVATE KEY' not in shared


def test_known_user_computer_and_root_markers_are_case_insensitive():
    redactor = DiagnosticRedactor(('FixtureUser', 'Fixture-PC', 'D:/Private Install'))
    raw = 'FixtureUser connected from FIXTURE-PC\nD:/Private Install/private-video.mp4\nFPS=30.2'
    shared = redactor.text(raw)
    assert 'fixtureuser' not in shared.casefold()
    assert 'fixture-pc' not in shared.casefold()
    assert 'private-video' not in shared
    assert 'FPS=30.2' in shared


def test_unknown_objects_do_not_expose_repr_and_nulls_are_retained():
    class PrivateObject:
        def __repr__(self):
            pytest.fail('Private object repr must never be invoked')
    assert DiagnosticRedactor().value({'value': PrivateObject(), 'token': None}) == {
        'value': REDACTED, 'token': None}


def test_redaction_is_stable_for_already_redacted_values():
    redactor = DiagnosticRedactor()
    once = redactor.value({'token': 'fixture-secret', 'notes': '192.0.2.10 frames=100'})
    assert redactor.value(once) == once
