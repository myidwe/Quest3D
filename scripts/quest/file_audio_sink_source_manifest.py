"""Record exact sources read by file sink checks; detect concurrent source edits."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
NATIVE = 'third_party/nightfall/addons/nightfall-stream/src/audio/'
FILES = [NATIVE + name for name in ('audio_device_callbacks.h', 'file_audio_sink.h', 'file_audio_sink.cpp',
    'miniaudio_backend.h', 'miniaudio_backend.cpp', 'file_audio_protocol.h', 'audio_startup.h', 'audio_cleanup.h',
    'audio_renderer.h', 'audio_renderer.cpp', 'opus_decoder.h', 'opus_decoder.cpp')]
FILES += ['third_party/nightfall/addons/nightfall-stream/include/miniaudio.h']
FILES += ['scripts/quest/' + name for name in ('test_file_audio_sink.cpp','test_file_audio_sink.sh',
    'verify_file_audio_sink_native.py','file_audio_sink_source_manifest.py','test_audio_backend_startup.cpp','test_audio_backend_cleanup.cpp')]
FILES += ['scripts/quest/audio_' + suite + '_fixture/' + name for suite in ('startup','cleanup') for name in ('probe.cpp','test.gd')]

def source_hashes():
    return [{'path': name, 'sha256': hashlib.sha256((ROOT/name).read_bytes()).hexdigest()} for name in FILES]

if __name__ == '__main__':
    current = source_hashes()
    with Path(sys.argv[1]).open('x',encoding='utf-8') as output:
        json.dump(current,output,indent=2); output.write('\n')
    if len(sys.argv) > 2 and current != json.loads(Path(sys.argv[2]).read_text()):
        raise SystemExit('Sources changed during verification; preserve this run and repeat frozen sources')
