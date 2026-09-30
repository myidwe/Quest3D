"""Desktop system-audio policy; inspection never changes Windows output.

The host owns temporary routing and its durable watchdog journal. This module
selects a validated route and recovers only that host's journal after shutdown.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

from .assets import sha256_file
from .paths import ROOT

OUTPUTS = {'pc': 'PC · 기본', 'quest': 'Quest only', 'both': 'PC + Quest'}
PROBE_SHA256 = '1511eb13904a5164f8b6b7bce15abbb76115fb42ecd9cdc7a2b1548014657944'
ENDPOINT = re.compile(r'\{0\.0\.0\.00000000\}\.\{[0-9a-fA-F-]{36}\}')


def validate_output(mode):
    if not isinstance(mode, str) or mode not in OUTPUTS:
        raise ValueError('Sound 출력은 PC, Quest only, PC + Quest 중 선택')
    return mode


def inventory(root=ROOT):
    """Use the shipped, pinned read-only Core Audio probe without PCM capture."""
    probe = Path(root) / 'native/audio/dist/audio_probe.exe'
    if not probe.is_file() or sha256_file(probe) != PROBE_SHA256:
        raise ValueError('오디오 확인 도구 없음 · 설치 파일 확인 필요')
    result = subprocess.run([str(probe)], capture_output=True, timeout=4,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError('Windows 출력 장치 확인 실패')
    data = json.loads(result.stdout.decode('utf-8-sig'))
    if (not isinstance(data, dict) or data.get('read_only') is not True
            or data.get('renders_audio') is not False or data.get('saves_pcm') is not False
            or data.get('defaults_unchanged') is not True
            or data.get('defaults_before') != data.get('defaults_after')):
        raise ValueError('Windows 출력 장치 변경 중 · 다시 선택 필요')
    return data


def plan(mode, data=None, *, root=ROOT):
    validate_output(mode)
    result = dict(audio_output=mode, audio_enabled=mode != 'pc', audio_endpoint=None)
    if mode == 'pc':
        return result  # No devices, driver or COM access needed for the default.
    data = inventory(root) if data is None else data
    rows = data.get('active_render_endpoints')
    defaults = data.get('defaults_before')
    if not isinstance(rows, list) or not isinstance(defaults, dict):
        raise ValueError('Windows 출력 장치 확인 실패')
    devices = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not ENDPOINT.fullmatch(row['id']):
            raise ValueError('오디오 장치 ID 확인 실패')
        if row['id'] in devices or not isinstance(row.get('name'), str):
            raise ValueError('중복 오디오 장치 확인 필요')
        volume = row.get('volume_scalar')
        if type(row.get('muted')) is not bool or type(volume) not in (int, float) or not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError('오디오 장치 음량 확인 실패')
        devices[row['id']] = row
    if any(not isinstance(defaults.get(role), dict) for role in ('console', 'multimedia', 'communications')):
        raise ValueError('Windows 기본 출력 역할 확인 실패')
    ids = [defaults[role].get('id') for role in ('console', 'multimedia', 'communications')]
    if any(not isinstance(key, str) or key not in devices for key in ids):
        raise ValueError('Windows 기본 출력 장치 없음')
    if mode == 'both':
        return result
    if ids[0] != ids[1]:
        raise ValueError('Windows 기본 출력 역할 불일치 · 소리 설정 확인 필요')
    virtual = [row for row in rows if row['name'] == 'Steam Streaming Speakers'
               or row['name'].endswith('(Steam Streaming Speakers)')]
    if len(virtual) != 1:
        raise ValueError('Quest only · Steam Streaming Speakers 설치·활성화 필요')
    target = virtual[0]
    if target['muted'] or target['volume_scalar'] == 0:
        raise ValueError('Steam Streaming Speakers 음소거 해제·음량 확인 필요')
    if target['id'] in ids:
        raise ValueError('먼저 Windows 기본 출력을 PC 스피커·헤드폰으로 설정')
    return {**result, 'audio_endpoint': target['id']}


def options(root=ROOT):
    try:
        data, error = inventory(root), None
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        data, error = None, str(exc)
    rows = []
    for mode, label in OUTPUTS.items():
        reason = ''
        if mode != 'pc':
            try:
                if error:
                    raise ValueError(error)
                plan(mode, data)
            except ValueError as exc:
                reason = str(exc)
        rows.append(dict(id=mode, label=label, available=not reason, reason=reason))
    return rows


def recover_stopped_host(root, runtime, record):
    """Confirm restoration of this recorded lifetime, never a guessed endpoint."""
    from .audio_recovery import WindowsAudioBackend, recover_journal
    if not record.get('audio_directory'):
        if record.get('audio_output', 'pc') == 'quest':
            raise ValueError('오디오 복구 기록 없음 · 진단 확인 필요')
        return {'status': 'no_route'}
    runtime = Path(runtime).resolve(strict=True)
    directory = Path(record['audio_directory']).resolve(strict=True)
    if directory.parent != runtime or not re.fullmatch(r'audio-[0-9a-f]{32}', directory.name):
        raise ValueError('오디오 복구 경로 불일치')
    journal = directory / 'route.json'
    if not journal.exists():
        return {'status': 'no_route'}  # Native COM changes require an existing journal.
    expected = (record['process_id'], record['owner_creation_filetime'])
    with WindowsAudioBackend(allow_changes=True) as backend:
        # The native host and watchdog share this lock. Let a concurrent,
        # legitimate restoration finish before reporting a persistent fault.
        for attempt in range(10):
            result = recover_journal(journal, backend, apply=True,
                                     allowed_root=Path(root)/'artifacts', expected_owner=expected)
            if result['status'] != 'refused' or 'locked or inaccessible' not in result.get('error', ''):
                break
            if attempt < 9:
                time.sleep(.15)
    if result['status'] not in ('restored', 'already_restored'):
        raise ValueError('PC 소리 복구 확인 필요 · 진단 기록 ' + str(journal))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=tuple(OUTPUTS), required=True)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps(plan(args.mode, root=args.root), ensure_ascii=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
