"""Bounded actual desktop profile/mode round trip; final state is comfort 3D.

Run before connecting the host. Saves real IPC pixels and acknowledged status;
does not interpret pixel differences as optical improvement.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from quest3d.session_control import read_json, send_control


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    before = read_json(args.session / 'status.json')
    assert before['running'] and before['disparity_profile'] == 'comfort'
    assert before['requested_mode'] == '3d' and before['disparity'] > 0
    report = {'before': before, 'steps': [], 'status': 'IN_PROGRESS'}
    def save():
        (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    save()
    try:
        for label, control, mode, profile in [
            ('linear', {'disparity_profile': 'linear'}, '3d', 'linear'),
            ('comfort', {'disparity_profile': 'comfort'}, '3d', 'comfort'),
            ('original-2d', {'mode': '2d'}, '2d', 'comfort'),
            ('restored-comfort', {'mode': '3d'}, '3d', 'comfort'),
        ]:
            result = send_control(args.session, **control)
            step = {'label': label, 'request': result}
            report['steps'].append(step)
            save()
            deadline = time.monotonic() + 15
            while True:
                status = read_json(args.session / 'status.json')
                if status.get('applied_request') == result['request_id']:
                    break
                if status.get('rejected_request') == result['request_id']:
                    raise RuntimeError(status.get('error'))
                if time.monotonic() >= deadline:
                    raise TimeoutError('Profile/mode was not acknowledged')
                time.sleep(.05)
            assert status['session_id'] == before['session_id']
            assert status['stream_epoch'] == before['stream_epoch']
            assert status['effective_mode'] == mode
            assert status['disparity'] == before['disparity']
            assert status['disparity_profile'] == profile
            actual = 'linear' if mode == '2d' else profile
            assert status['effective_disparity_profile'] == actual
            assert status['effective_convergence'] == (None if mode == '2d' else (.625 if profile == 'comfort' else .5))
            step['status'] = status
            command = [sys.executable, '-B', 'native/diagnostics/read_live_quality_sbs.py',
                       '--session', str(args.session), '--output', str(args.output / label)]
            if mode == '3d':
                command.append('--require-3d')
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
            snapshot = read_json(args.output / label / 'snapshot.json')
            assert snapshot['header']['stream_epoch'] == before['stream_epoch']
            assert snapshot['eye_pixels_identical'] == (mode == '2d')
            step['snapshot'] = snapshot
            save()
        report['status'] = 'PASS'
    except BaseException as exc:
        report.update(status='FAIL', error=repr(exc))
        raise
    finally:
        report['after'] = read_json(args.session / 'status.json')
        save()
    print(json.dumps({'status': report['status'], 'steps': [s['label'] for s in report['steps']],
                      'epoch': before['stream_epoch'], 'output': str(args.output)}))


if __name__ == '__main__':
    main()
