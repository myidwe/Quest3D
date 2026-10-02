# Sterevi

**익숙한 PC 화면을 Quest에서 입체로**

Windows 화면을 Quest의 큰 가상 화면에서 **스테레오 3D**로 볼 수 있는 오픈소스 앱입니다. 기존 브라우저나 플레이어를 그대로 사용하면서 **2D와 3D를 바로 전환**할 수 있습니다. AI 처리는 PC에서 실행합니다.

[English](README.en.md) · **0.1.3-preview**

## 소개 영상

https://github.com/user-attachments/assets/0a0dac44-79db-49f7-911c-ecf5be4d2878

[세로 영상](docs/media/VERTICAL.md) · [영상 정보](docs/media/README.md)

<sub>실제 앱 UI와 로컬 AI의 깊이·좌우 영상 생성 과정을 소개합니다. 자동차 돌출 장면은 입체 감상을 설명하는 광고 연출입니다.</sub>

<details>
<summary>이미지로 보는 처리 흐름</summary>

![PC 화면의 깊이를 AI로 추정하고 좌우 영상을 만들어 Quest에서 입체로 보는 과정](docs/assets/quest3d-workflow-v3.png)

<sub>기능을 설명하기 위해 생성한 이미지입니다. 실제 앱 화면이나 AI 변환 결과는 아닙니다. [이미지 제작 정보](docs/assets/README.md)</sub>

</details>

## 시작 전 확인

**Windows x64 · NVIDIA Turing(sm75) · Quest 2 / 3 · 16:9 모니터 · 같은 공유기의 로컬 네트워크**

**RTX 2060 SUPER 8GB / Windows 11**에서 개발하고 성능을 측정했습니다. 다른 Turing GPU의 메모리 사용량·인코더 동작·성능은 개별적으로 확인하지 않았습니다. 다른 NVIDIA 세대와 AMD·Intel GPU는 현재 지원하지 않습니다. [지원 범위와 확인한 환경](docs/RELEASE_0.1.3_PREVIEW.md)

## 다운로드

| Windows 앱 | Quest 앱 |
|:---|:---|
| **[Desktop Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Desktop-Setup-0.1.3-preview.exe)** | **[Quest Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-Setup-0.1.3-preview.exe)** |
| PC 앱 설치 | USB로 Quest 앱 설치 |
| | **[APK 다운로드](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-0.1.3-preview.apk)** · 별도 설치 도구 사용 |

**두 EXE 파일은 Windows에서 실행합니다.** 처음 PC 앱을 설치할 때는 인터넷에 연결해야 하며, 실행에 필요한 프로그램·GPU 라이브러리·AI 모델 등 수 GB 분량의 파일을 내려받습니다. 이후 AI 처리는 로컬에서 실행하며, 앱 사용료·구독료·클라우드 추론료는 없습니다.

Windows 설치 파일에는 아직 코드 서명이 없어 경고가 뜨거나 실행이 차단될 수 있습니다. [설치 조건](docs/EXE_INSTALLERS.md#권한과-windows-조건)

## 설치와 연결

1. **Desktop Setup**을 실행하고 **설치 → 연결 허용**을 누른 뒤 설치 창을 닫습니다.
2. Quest의 **개발자 모드**를 켜고 **USB 디버깅·ADB**를 준비한 뒤 **Quest Setup**으로 앱을 설치합니다.
3. **Sterevi Desktop → PC 시작**을 누릅니다. Quest에서는 **Scan Network → PC 선택 → Pair**를 누릅니다.
4. Quest에 표시된 **PIN을 PC 앱에 입력하고 연결을 승인**한 뒤 Quest의 **Connect**를 누릅니다.

자세한 준비 방법과 연결 순서는 [처음 설치와 연결](docs/GETTING_STARTED.md)에서 확인하세요.

다음부터는 **Sterevi Desktop → PC 시작 → Quest의 Connect**만 누르면 됩니다.

## 주요 기능

- 기존 브라우저·프로그램 화면을 Quest로 스트리밍하고 2D/3D 전환
- Depth로 입체감 조절, 윤곽 안정화로 가장자리의 과한 양안 차이 완화
- 화면 크기·거리·위치·곡률·색감·선명도 조절, 화면 설정 저장
- Quest 2 / 3 화질 프로필, H.264 / HEVC, PC·Quest 소리 출력 선택

[사용 방법](docs/DESKTOP_USER_GUIDE.md) · [문제 해결](docs/DESKTOP_USER_GUIDE.md#문제-해결) · [문서 전체](docs/README.md) · [문의·오류 제보](https://github.com/myidwe/Sterevi/issues)

## 앱 화면

**Windows · Display**

<img src="docs/assets/sterevi-desktop.png" alt="Sterevi Windows 앱의 Display 화면" width="640">

| Quest · 시작 | Quest · 화면 설정 |
|:---:|:---:|
| <img src="docs/assets/sterevi-quest-home.png" alt="Sterevi Quest 시작 화면" width="420"> | <img src="docs/assets/sterevi-quest-settings.png" alt="Sterevi Quest Display 설정 화면" width="420"> |

<sub>실제 앱 UI를 샘플 설정으로 PC에서 표시한 화면입니다. 헤드셋에서 촬영한 사진이나 AI 변환 품질을 보여 주는 이미지는 아닙니다. [캡처 정보](docs/assets/README.md)</sub>

## Preview 안내

PC 조작에는 Windows 마우스·키보드를 사용합니다. 3D에서는 얇은 물체나 가려진 배경의 윤곽이 양쪽 눈에서 다르게 보일 수 있습니다. DRM이나 화면 캡처 차단이 적용된 콘텐츠는 정상 표시를 보장하지 않습니다.

소리는 기본적으로 PC에서 재생합니다. **Quest only**를 사용하려면 PC에 **Steam Streaming Speakers**가 설치되어 있고 활성화되어 있어야 합니다. 다른 PC에서의 설치, Quest 2에서 이번 앱의 설정 화면이 정상적으로 작동하는지, 영상과 소리의 시간차가 어느 정도인지, 장시간 사용해도 안정적인지 등 아직 확인하지 못한 내용은 [배포 안내](docs/RELEASE_0.1.3_PREVIEW.md)에 정리했습니다.

<details>
<summary>OWL3D와의 관계</summary>

Sterevi는 PC 화면을 실시간으로 2D에서 3D로 변환해 감상하는 독립 오픈소스 프로젝트입니다. [OWL3D Link](https://www.owl3d.com/blog/releasesv203)와 사용 목적이 일부 겹치지만 OWL3D의 공식 버전이나 포크는 아니며, 제휴 관계도 없습니다. 기능과 화질이 동일하다는 뜻은 아닙니다.

</details>

<details>
<summary>개발·수동 설치·검증 자료</summary>

- [전체 배포 파일](https://github.com/myidwe/Sterevi/releases/tag/v0.1.3-preview): 수동 설치 ZIP, 네이티브 구성 요소의 소스를 포함한 Source ZIP, 체크섬·검증 보고서
- [설치·업데이트·복구·제거](docs/DISTRIBUTION.md) · [설정·권한](docs/SETUP_PERMISSIONS_2026-09-30.md)
- [구조·빌드·테스트](docs/BUILDING.md) · [기여 안내](CONTRIBUTING.md) · [제품 범위](docs/PRODUCT_SCOPE.md)
- [개인정보 수정 내역](docs/PRIVACY_REMEDIATION_2026-10-01.md) · [의존성·대응 소스 감사](docs/DEPENDENCY_AUDIT_2026-09-30.md)

GitHub의 **Code → Download ZIP**에서는 프로젝트 소스를 받습니다. 배포된 바이너리를 재빌드하는 데 필요한 네이티브 대응 소스는 같은 릴리스의 **Source ZIP**에 포함되어 있습니다. 송출 FPS와 AI가 새 영상을 만드는 속도는 다르며, 모든 장면에서 60FPS를 보장하지 않습니다.

</details>

프로젝트 코드: **[GPL-3.0](LICENSE)** · 구성 요소·모델 조건: [Third-party notices](THIRD_PARTY_NOTICES.md)
