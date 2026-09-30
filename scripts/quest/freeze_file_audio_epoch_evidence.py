"""Freeze existing successful component evidence without claiming device playback."""
from pathlib import Path
import hashlib
import json
import shutil
import sys

root = Path(__file__).resolve().parents[2]
out = (root / sys.argv[1]).resolve()
if not out.is_relative_to(root / 'artifacts/quest') or (out / 'source-freeze.json').exists():
    raise ValueError('Expected new source freeze inside Quest artifacts')
report = json.loads((out / 'verification.json').read_text(encoding='utf-8'))
assert report['passed'] and len(report['results']) == 9
hash_of = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
for line in (out / 'source-before.sha256').read_text(encoding='utf-8').splitlines():
    expected, filename = line.split('  ', 1)
    relative = filename.split('/quest_to_3d/', 1)[1]
    assert hash_of(root / relative) == expected, relative
android = root / 'artifacts/quest/file-audio-epoch-review/android-qshk0u65/verification.json'
compile_report = json.loads(android.read_text(encoding='utf-8'))
assert compile_report['passed'] and compile_report['source_unchanged']
for row in compile_report['source_files']:
    assert hash_of(root / row['path']) == row['sha256'], row['path']

base = 'third_party/nightfall/addons/nightfall-stream/'
paths = [base + 'src/audio/' + name for name in (
    'file_audio_epoch_session.h', 'file_audio_epoch_session.cpp', 'file_audio_protocol.h',
    'file_audio_sink.h', 'file_audio_sink.cpp', 'audio_cleanup.h', 'audio_startup.h',
    'audio_device_callbacks.h', 'miniaudio_backend.h', 'miniaudio_backend.cpp')]
paths += [base + 'src/network/' + name for name in (
    'file_audio_transport.h', 'file_audio_transport.cpp', 'curl_http_client.h', 'curl_http_client.cpp')]
paths += [base + 'include/miniaudio.h']
paths += ['scripts/quest/test_file_audio_epoch_session' + ext for ext in ('.cpp', '.py', '.sh')]
paths += ['scripts/quest/freeze_file_audio_epoch_evidence.py']
snapshot = out / 'source-snapshot'
sources = []
for relative in paths:
    source = root / relative
    target = snapshot / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    digest = hash_of(source)
    assert hash_of(target) == digest
    sources.append(dict(path=relative, sha256=digest))
evidence = [out / 'verification.json', out / 'source-before.sha256', out / 'source-after-check.log',
            out / 'build.log', out / 'test.log', out / 'epoch-test', android]
(out / 'source-freeze.json').write_text(json.dumps(dict(passed=True,
    scope='Production TLS and Null callback component; no physical audio, Quest or product A/V',
    sources=sources, evidence=[dict(path=str(path.relative_to(root)).replace('\\', '/'), sha256=hash_of(path)) for path in evidence]),
    indent=2) + '\n', encoding='utf-8')
print(json.dumps(dict(passed=True, files=len(sources), manifest=str(out / 'source-freeze.json'))))
