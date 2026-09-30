"""Record the completed private component checks; never asserts app/device READY."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
out = root / 'artifacts/quest/moonlight-audio-startup'
overlay = root / 'third_party/nightfall/addons/nightfall-stream/vcpkg-overlay/moonlight-common-c'

def info(path):
    data = path.read_bytes()
    return {'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}

logs = {}
for name in ['test.log', 'test-release.log']:
    path = out / name
    text = path.read_text(encoding='utf-8')
    assert text.count('PASS all audio startup component cases;') == 2
    assert 'PASS production binary has no endpoint grant' in text
    assert 'PASS held actual receive-worker exit callback' in text
    assert 'ERROR: AddressSanitizer' not in text and 'runtime error:' not in text
    logs[name] = {**info(path), 'pass_lines': sum(line.startswith('PASS ') for line in text.splitlines())}

traces = {}
for name in ['Debug-fixture', 'Debug-production', 'Release-fixture', 'Release-production']:
    path = out / f'trace-{name}.jsonl'
    events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    assert events
    terminal = set()
    for event in events:
        if event['event'] == 'core_state':
            bits = event['bits']
            assert bits & 0x10  # Endpoint remains unconfirmed in EVERY observation.
            if event['serial'] in terminal:
                assert not bits & 0xf
            if bits & 0xf00:
                terminal.add(event['serial'])
                assert not bits & 0xf
    traces[name] = {**info(path), 'events': len(events),
                    'actual_opus_decodes': sum(e['event'] == 'actual_opus_decode' for e in events),
                    'terminal_serials': sorted(terminal)}

source_files = [overlay / '0004-quest3d-audio-startup.patch', overlay / 'quest3d/AudioStartup.h',
                overlay / 'portfile.cmake', overlay / '0003-quest3d-connection-callback-drain.patch']
for filename in ['AudioStream.c', 'RtpAudioQueue.c', 'RtpAudioQueue.h', 'Connection.c',
                 'Limelight.h', 'Limelight-internal.h', 'AudioStartup.h']:
    source_files.append(out / 'source/src' / filename)
test_files = [root / 'scripts/quest' / name for name in [
    'prepare_moonlight_audio_startup.py', 'test_moonlight_audio_startup.sh',
    'test_moonlight_audio_startup_release.sh', 'compile_moonlight_audio_startup_android.sh',
    'moonlight_audio_startup_fixture/CMakeLists.txt', 'moonlight_audio_startup_fixture/test_audio_startup.c']]
proof = {
    'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
    'scope': 'Pinned actual Moonlight core audio startup and first RTP/FEC block component only',
    'source_commit': '7b026e77be62175104640e7e722b758df6d3d0d7',
    'status': 'component_checks_passed_production_fresh_denied',
    'source': [info(p) for p in source_files],
    'tests': [info(p) for p in test_files],
    'logs': logs, 'traces': traces,
    'host_binaries': [info(out / build / executable) for build in ['build', 'build-release']
                      for executable in ['audio-startup-test', 'audio-startup-production-denial-test']],
    'android_objects': [info(out / 'android-objects' / (unit + '.android.o'))
                        for unit in ['AudioStream', 'RtpAudioQueue', 'Connection']],
    'document': info(root / 'docs/build/MOONLIGHT_AUDIO_STARTUP.md'),
    'actual_components': ['AudioStream whole TU', 'RtpAudioQueue', 'Platform pthread/LBQ/UDP',
                          'RS FEC', 'libopus 1.3.1 encoder/decoder and independent reference decoder'],
    'test_doubles': ['renderer callbacks (actual libopus decode inside)',
                     'compile-time private endpoint policy injection',
                     'thread-create/malloc/receive failure injection and held lifecycle barriers'],
    'not_verified': ['authenticated endpoint profile/one-use coordinator', 'all-retired endpoint history',
                     'same-host old-key/FEC packet exclusion', 'host FilePCM offer/send READY gate',
                     'combined AudioRenderer/Miniaudio watch', 'AAudio/OpenSL device callback or speaker',
                     'Quest headset', 'Nightfall full ABI relink/APK deployment'],
    'production_fresh_mode': 'unconditionally rejected without private fixture macro; no runtime enable API',
}
target = out / 'verification.json'
target.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'verification': info(target), 'logs': logs}, ensure_ascii=False, indent=2))
