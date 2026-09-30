# Contributing

현재 목표는 Windows 화면의 안정적인 Quest 2·Quest 3 감상입니다. 기능 범위와 지원 장비는 [README](README.md), 빌드는 [BUILDING](docs/BUILDING.md)을 기준으로 합니다.

## Changes

- 관련 없는 파일·사용자 설정·모델 가중치·기존 실험 결과 보존
- 의존성·모델·GPU 지원 변경 시 버전·해시·라이선스와 실제 검증 기록 첨부
- FPS는 새 3D 생성·반복 게시·인코딩·Quest 수신·헤드셋 표시율 구분
- 평균 화질 외 얇은 경계·양안 일치·장면 전환·움직임도 확인
- GPU·Quest가 없는 테스트 결과를 실기 검증으로 표시하지 않음
- 실패 재현, 수정, 재검증과 남은 제한을 PR에 기록

## Tests

GPU가 없어도 배포 경계 검사를 실행할 수 있습니다. 아래 검사는 pytest 8.4.2와 Python 3.12.6에서 실행합니다.

```powershell
python -m pytest tests/test_release_bundle.py tests/test_release_ui.py tests/test_publication_export.py -q
```

전체 tests에는 로컬 실험 자료·native 도구·CUDA·Quest 환경이 필요한 검사도 포함됩니다. 이 세 파일의 통과만으로 실제 스트리밍·오디오·착용 화질 검증을 대체하지 않습니다.

## Issues

Windows 빌드, GPU·드라이버, Quest 모델, 앱 버전, 재현 순서와 기대/실제 동작을 적습니다. 로그는 공개 전에 PC 주소·기기 일련번호·개인 경로를 제거합니다. 화면 캡처는 공개 권한이 있는 자료만 첨부합니다. 페어링 인증서·계정 파일·서명키·원본 미디어는 보내지 않습니다.
