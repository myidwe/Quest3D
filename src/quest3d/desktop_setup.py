"""Installation layout validation; development installs keep their existing paths."""
from pathlib import Path, PurePosixPath
import json
import re


def distribution_layout(root: Path, *, host_sha: str, runtime: str, capture: str) -> dict:
    path = root / 'config/distribution.json'
    if not path.exists():
        return {'distributed': False, 'runtime': root / runtime, 'capture': root / capture}
    if '#' in str(root):
        raise ValueError('설치 폴더 이름에 #을 사용할 수 없습니다. #이 없는 폴더에 설치해 주세요.')
    value = json.loads(path.read_text('utf-8-sig'))
    if not isinstance(value, dict) or value.get('schema') != 1 or value.get('host_sha256') != host_sha:
        raise ValueError('설치 정보의 전송 서버 버전이 다릅니다. 검증된 설치 파일로 복구해 주세요.')
    result = {'distributed': True}
    for key, field in (('runtime', 'host_runtime'), ('capture', 'hdr_package')):
        text = value.get(field)
        if not isinstance(text, str) or '\\' in text or ':' in text or any(c in text for c in '#\r\n'):
            raise ValueError('설치 경로 정보가 올바르지 않습니다.')
        relative = PurePosixPath(text)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('설치 경로가 앱 폴더를 벗어났습니다.')
        target = (root / text).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError('설치 경로가 앱 폴더를 벗어났습니다.')
        result[key] = target
    if result['runtime'].parent != (root / 'artifacts/host').resolve() or not re.fullmatch(r'runtime-[a-zA-Z0-9_-]+', result['runtime'].name):
        raise ValueError('전송 서버 설치 위치가 올바르지 않습니다.')
    return result
