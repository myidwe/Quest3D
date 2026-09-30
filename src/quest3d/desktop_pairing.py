"""PIN submission through the existing exact-host/TLS-pinned local helper."""
import json
import subprocess


def approve_pin(root, pwsh, environment, pin, *, run=subprocess.run):
    command = [str(pwsh), '-NoProfile', '-File', str(root/'native/host/pairing-host.ps1')]
    options = dict(cwd=root, env=environment, capture_output=True, text=True, encoding='utf-8',
                   timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    def invoke(argv, payload=None):
        try:
            response = run(argv, input=payload, **options)
            if response.returncode not in (0, 2):
                raise ValueError()
            value = json.loads(response.stdout.lstrip('\ufeff'))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except Exception:
            # Never propagate stdout/stderr/command exception text containing stdin.
            raise RuntimeError('연결 승인 결과를 확인하지 못했습니다. 자동 재시도하지 않았습니다. Quest 상태를 확인해 주세요.') from None
    pending = invoke(command)
    requests = pending.get('eligible_requests')
    if requests is None:  # Existing strict address helper.
        requests = [r for r in pending.get('pending', []) if r.get('address') == pending.get('quest_address')]
    if not isinstance(requests, list) or len(requests) != 1:
        raise RuntimeError('연결 요청이 없거나 여러 개입니다. Quest 한 대에서 새 PC 연결을 열고 PIN을 다시 입력해 주세요.')
    request_id = requests[0].get('id')
    if not isinstance(request_id, str) or len(request_id) != 32 or any(c not in '0123456789abcdef' for c in request_id.lower()):
        raise RuntimeError('Quest 연결 요청 정보를 확인하지 못했습니다.')
    return invoke(command + ['-Submit', '-PairingId', request_id, '-PinFromStdin'], json.dumps({'pin': pin}))
