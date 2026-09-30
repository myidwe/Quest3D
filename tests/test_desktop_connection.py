"""Connection badges must refer to this host lifetime, including rolling logs."""
from quest3d.desktop_backend import DesktopController
from quest3d.session_control import atomic_json


def test_prior_host_connection_cannot_mark_restarted_host_connected(tmp_path):
    c = DesktopController(tmp_path, start_worker=False)
    base = tmp_path/'artifacts/host/dev'
    atomic_json(base/'process.json', dict(process_id=10, owner_creation_filetime='100',
                                         started_at='2026-09-11T10:00:00.500+09:00'))
    (base/'sunshine.log').write_text('[2026-09-11 09:59:00.999]: Info: CLIENT CONNECTED\n')
    assert c._connection(True)[0] is None
    (base/'sunshine.log').write_text('[2026-09-11 10:00:01.000]: Info: CLIENT CONNECTED\n')
    assert c._connection(True)[0] is True
    (base/'sunshine.log').write_text('[2026-09-11 10:01:00.000]: Info: video frame\n')
    assert c._connection(True)[0] is True  # A rolling tail must retain the observation.
    (base/'sunshine.log').write_text('[2026-09-11 10:02:00.000]: Info: CLIENT DISCONNECTED\n')
    assert c._connection(True)[0] is False
    assert c._connection(False)[0] is False


def test_pid_reuse_resets_cached_connection(tmp_path):
    c = DesktopController(tmp_path, start_worker=False)
    base = tmp_path/'artifacts/host/dev'
    owner = dict(process_id=10, owner_creation_filetime='100', started_at='2026-09-11T10:00:00.500+09:00')
    atomic_json(base/'process.json', owner)
    (base/'sunshine.log').write_text('[2026-09-11 10:00:01.000]: Info: CLIENT CONNECTED\n')
    assert c._connection(True)[0] is True
    atomic_json(base/'process.json', {**owner,'owner_creation_filetime':'101',
                                    'started_at':'2026-09-11T11:00:00.500+09:00'})
    assert c._connection(True)[0] is None
