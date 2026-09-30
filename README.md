# Quest3D

Windows 데스크톱을 Meta Quest의 큰 화면으로 보고, 로컬 AI로 2D와 스테레오 3D를 전환하는 앱

**공개 준비 중 · Windows/NVIDIA Turing 전용 Preview**

[English](README.en.md)

현재 개발 PC와 Quest 2·Quest 3에서 실제 캡처·AI 깊이·좌우 영상·헤드셋 표시를 연결해 사용했습니다. 최신 공개 설치 파일은 아직 게시하지 않았습니다. 첫 공개는 제한된 장비를 대상으로 한 `0.1.0-preview`를 계획합니다.

## Download

공개 후 이 저장소의 **Releases**에서 아래 두 파일을 받습니다. GitHub의 **Code → Download ZIP**은 개발 소스이며 설치 파일이 아닙니다.

| 파일 | 용도 |
|---|---|
| `Quest3D-Desktop-<version>.zip` | Windows 설치창과 PC 앱 |
| `Quest3D-Quest-<version>.zip` | Quest APK와 USB 설치창 |
| `Quest3D-Source-<version>.zip` | native 구성 요소를 포함한 대응 소스 |
| `SHA256SUMS.txt` | 다운로드 파일 무결성 확인 |

저장소 주소와 실제 다운로드 링크는 공개 시 확정합니다. 기존 개발용 ZIP을 최신 배포판으로 안내하지 않습니다.

## Quick start

1. PC ZIP 전체 압축 해제 → **Install-Quest3D.cmd → 설치 → 연결 허용**
2. Quest 개발자 모드·USB 디버깅 승인 → **Install-Quest.cmd**로 APK 설치
3. PC와 Quest를 같은 사설망에 연결
4. **Quest3D Desktop → PC 시작**
5. Quest의 **Scan Network → PC 선택 → Pair**, PC 앱에서 PIN 승인
6. 이후 **PC 시작 → Quest Connect**

처음 설치에는 인터넷과 Python·GPU 라이브러리·모델 다운로드가 필요합니다. 이후 AI 추론은 PC에서 실행합니다. 사용료·구독료·클라우드 추론료는 없습니다. 사용 시 Codex·WSL·개발 도구가 필요하지 않습니다.

[설치·업데이트·문제 해결](docs/DISTRIBUTION.md) · [사용 방법](docs/DESKTOP_USER_GUIDE.md)

## Features

- 선택 모니터의 기존 브라우저·프로그램 화면 스트리밍
- 2D/3D 전환, Depth 미세 조절, 윤곽 안정화
- DAv2 Small 기본 모델, DAD Small 선택 비교
- Standard / Quality · Preview AI 품질 설정
- Quest 화면 크기·거리·위치·곡률·색감·선명도·보기 저장
- 기본 수평 정렬, 별도 Free 정렬, 설정창 이동과 포인터 조절
- H.264/HEVC 전송, Quest 2·Quest 3 출력 프로필
- PC / Quest only / PC + Quest 소리 출력
- Windows 시작·중지·트레이·PIN 연결·진단

소리는 **PC 출력이 기본**입니다. Quest only는 별도로 설치된 활성 Steam Streaming Speakers가 필요합니다. 이 앱은 해당 드라이버를 배포하거나 자동 설치하지 않습니다. 장치가 없으면 PC 또는 PC + Quest를 사용합니다.

## Requirements

| 항목 | 현재 범위 |
|---|---|
| PC | Windows x64, 실측 Windows 11 |
| GPU | 현재 CUDA 경로는 NVIDIA Turing `sm75` 전용 |
| 실측 GPU | RTX 2060 SUPER 8GB |
| 헤드셋 | Meta Quest 2 / Quest 3 |
| 연결 | 동일 사설 LAN, USB는 설치·진단용 |
| 모니터 | 현재 16:9 |
| Quest 2 출력 | 눈별 1920×1080, Full SBS 3840×1080 |
| Quest 3 출력 | 눈별 2048×1152, Full SBS 4096×1152, HEVC |

RTX 20·GTX 16 계열도 개별 모델의 VRAM·인코더·성능을 모두 검증한 것은 아닙니다. 다른 NVIDIA 세대·AMD·Intel·ARM Windows는 현재 지원 대상으로 안내하지 않습니다. 더 좋은 GPU라는 이유만으로 호환된다고 가정하지 않습니다.

## Quality and limits

RTX 2060 SUPER의 2026-09-22 PC 처리 측정에서 DAD Standard는 새 3D 영상 **38.66개/s**, Quality · Preview는 **34.64개/s**였습니다. 같은 PC의 특정 설정·장면을 60초씩 측정한 결과이며, Quest 수신 FPS나 모든 장면의 최소 FPS를 보장하지 않습니다. 반복 송출 60회/s와 새 AI 영상 수는 다릅니다.

얇은 난간·머리카락·가려진 배경에는 윤곽 차이가 남을 수 있습니다. Quality는 일부 경계를 개선하지만 모든 장면에서 더 좋지는 않습니다. 불편하면 Depth를 낮추거나 2D로 전환합니다. DRM·캡처 차단 콘텐츠의 재생·3D 변환은 보장하지 않습니다.

Quest 포인터로 Windows 클릭·드래그·스크롤, 내장 영상·사진 플레이어, 원래 창 안의 선택 영역만 입체화, 별도 AV 동기화 보정, 완전한 공간 복원은 현재 제품 범위에 포함하지 않습니다. 최신 UI·오디오의 Quest 2 재검증, 사용자 청취·AV 오차·장시간 안정성, 다른 PC의 새 설치는 남은 검증입니다.

## Development

Python 3.12.6 / PyTorch 2.7.1+cu126 / Qt 6.8.3을 고정합니다. 운영 캡처는 `wc_cuda 0.1.2+quest2`이며 `uv.lock`의 `+quest1`과 구분합니다. 저장소 소스만 받으면 운영 host·capture 바이너리가 자동으로 생기지는 않습니다.

[구조·빌드·테스트](docs/BUILDING.md) · [기여 안내](CONTRIBUTING.md) · [공개 준비 현황](docs/OPEN_SOURCE_RELEASE_PLAN_2026-09-30.md)

## License

Quest3D 프로젝트 코드는 [GNU GPL v3](LICENSE)입니다. Sunshine·Nightfall 변경 소스와 각 구성 요소의 고지를 함께 제공합니다. 고정 DAv2 Small·DAD Small 가중치는 각각의 공식 카드에 명시된 Apache-2.0 조건을 따르며 설치 시 별도 다운로드합니다. 다른 모델·크기에 같은 조건을 적용하지 않습니다.

[Third-party notices](THIRD_PARTY_NOTICES.md) · [의존성·대응 소스 감사](docs/DEPENDENCY_AUDIT_2026-09-30.md)
