# Contributing

Sterevi는 Windows 화면을 Quest 2·Quest 3에서 입체로 감상할 수 있도록 개발하는 앱입니다. 기능과 지원 장비는 [README](README.md), 빌드 방법은 [빌드 문서](docs/BUILDING.md)를 확인해 주세요.

## Changes

- 변경과 관계없는 파일, 사용자 설정, 모델 가중치, 기존 실험 결과는 보존해 주세요.
- 의존성·모델·GPU 지원을 변경할 때는 버전, 해시, 라이선스와 실제 검증 결과를 함께 기록해 주세요.
- 성능을 보고할 때는 새 3D 프레임 생성, 같은 프레임의 반복 송출, 인코딩, Quest 수신, 헤드셋 표시율을 구분해 주세요.
- 화질은 얇은 물체의 윤곽, 좌우 영상의 일치, 장면 전환, 움직임까지 확인해 주세요.
- GPU나 Quest 없이 수행한 테스트를 실제 기기 검증으로 표시하지 마세요.
- PR에는 문제 재현 방법, 수정 내용, 재검증 결과와 남은 제한을 적어 주세요.

## Tests

GPU가 없어도 배포 파일과 설치 정책을 검사할 수 있습니다. 아래 명령은 pytest 8.4.2와 Python 3.12.6에서 실행합니다.

```powershell
python -m pytest tests/test_release_bundle.py tests/test_release_ui.py tests/test_publication_export.py -q
```

전체 테스트에는 로컬 실험 자료, 네이티브 빌드 도구, CUDA 또는 Quest가 필요한 검사도 포함되어 있습니다. 위 검사가 통과해도 실제 스트리밍·소리·착용 시 화질은 별도로 확인해야 합니다.

## Issues

이슈를 작성할 때는 Windows 빌드, GPU와 드라이버, Quest 모델, 앱 버전, 재현 순서를 적어 주세요. 기대한 동작과 실제로 일어난 동작도 함께 알려 주세요.

로그를 올리기 전에 PC 주소, 기기 일련번호, 개인 폴더 경로를 지워 주세요. 화면 캡처는 공개해도 되는 자료만 첨부하고, 페어링 인증서·계정 파일·서명키·원본 미디어는 첨부하지 마세요.

## 문서 작성 기준

- README는 기능, 시작 방법, 도움받는 곳, 기여 방법을 중심으로 작성합니다.
- 제목은 짧은 명사형으로, 설명은 합니다체로, 설치 절차는 사용자가 수행할 행동으로 씁니다.
- 한국어 어순과 문장 호응을 확인하고, 번역투·불필요한 명사 나열을 줄입니다. 조사를 지나치게 생략하지 말고 쉬운 말을 씁니다.
- GitHub·APK·EXE처럼 익숙한 용어와 Pair·Connect 등 실제 버튼 이름은 유지합니다.
- 지원 범위와 검증하지 못한 조건을 정확히 적고, 개인 정보나 개발 환경의 값을 넣지 않습니다.

이 기준은 [GitHub의 README 안내](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)와 [국립국어원의 『한눈에 알아보는 공공언어 바로 쓰기』](https://korean.go.kr/common/download.do?c_file_name=f595077c-82b4-42a5-8ab6-0c31fef547ff.pdf&file_path=etcData&o_file_name=%28%EA%B0%9C%EC%A0%95%ED%8C%90%29+%ED%95%9C%EB%88%88%EC%97%90+%EC%95%8C%EC%95%84%EB%B3%B4%EB%8A%94+%EA%B3%B5%EA%B3%B5%EC%96%B8%EC%96%B4+%EB%B0%94%EB%A1%9C+%EC%93%B0%EA%B8%B0%28%EC%9B%B9%EC%9A%A9%29+.pdf)를 참고해 Sterevi 문서에 맞게 정리했습니다.
