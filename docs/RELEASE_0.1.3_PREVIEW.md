# 0.1.3-preview · Sterevi

**[처음 설치와 연결](GETTING_STARTED.md)** · [사용 방법](DESKTOP_USER_GUIDE.md) · [문서 목차](README.md)

제품 이름을 **Sterevi · 스테레비**로 바꾼 업데이트입니다. PC 앱·설치 창·바로가기, Quest 앱 목록·시작 화면·설정 화면, GitHub 안내에 새 이름을 적용했습니다. 캡처·AI 처리·좌우 영상 합성·코덱·소리·모델·기본 화질·성능 설정은 그대로입니다.

## 다운로드

| Windows | Quest |
|:---|:---|
| [Desktop Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Desktop-Setup-0.1.3-preview.exe) | [Quest Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-Setup-0.1.3-preview.exe) |
| PC 앱 설치·업데이트 | Windows에서 USB로 Quest 앱 설치 |
| | [APK 다운로드](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-0.1.3-preview.apk) · 별도 설치 도구 사용 |

수동 설치 ZIP, 네이티브 구성 요소의 소스를 포함한 Source ZIP, `SHA256SUMS.txt`, 개인정보 검사·검증 보고서는 [같은 릴리스](https://github.com/myidwe/Sterevi/releases/tag/v0.1.3-preview)에서 받을 수 있습니다. 직접 내려받는 APK와 Quest Setup에 포함된 APK는 같은 파일입니다. 바이너리를 재빌드하려면 GitHub가 자동으로 제공하는 Source code ZIP 대신 전체 네이티브 대응 소스를 담은 **Sterevi-Source ZIP**을 사용하세요.

## 기존 사용자 업데이트

- PC 설치 창에서 기존 설치 폴더를 확인하고 **업데이트**를 선택합니다. 이름이 바뀌어도 모델·설정·페어링 정보를 지우지 않습니다. 설치 후 **Sterevi Desktop → PC 시작**으로 사용합니다.
- Quest APK는 **0.1.3-preview / versionCode 4**입니다. 패키지 `app.questto3d.client`와 서명 인증서는 기존 공개 앱과 같습니다. 앱을 먼저 삭제하지 말고 EXE 또는 `adb install -r`로 업데이트하세요.
- 기존 공개 앱의 설정·저장한 화면 설정·PC 페어링 정보를 유지하는 방식으로 업데이트합니다. 처음 설치한 사용자는 **Pair/PIN 승인**이 필요합니다. 이미 저장된 PC에는 **Connect**로 접속합니다.
- 내부 Python 모듈·설치 식별 정보·통신 이름·사용자 데이터 경로에는 이전 이름이 남을 수 있습니다. 기존 데이터와의 호환성을 유지하고 중복 실행을 막기 위한 것입니다. 사용자에게 표시되는 제품 이름은 Sterevi입니다.
- 이전 릴리스의 파일명·버전·태그는 당시 배포 기록으로 보존합니다.

## 지원과 검증 범위

Windows x64, NVIDIA Turing(sm75), Quest 2 / 3, 16:9 모니터를 대상으로 합니다. PC와 Quest는 같은 공유기의 신뢰할 수 있는 로컬 네트워크에 연결해야 합니다. 개발과 성능 측정은 RTX 2060 SUPER 8GB / Windows 11에서 진행했습니다. 다른 Turing GPU의 메모리 사용량·인코더 동작·성능과 다른 PC에서의 설치는 개별적으로 확인하지 않았습니다. 다른 NVIDIA 세대와 AMD·Intel GPU는 현재 지원하지 않습니다.

Windows 설치 파일에는 아직 코드 서명이 없어 SmartScreen 경고가 뜨거나, 회사·학교 PC의 보안 정책에 따라 실행이 차단될 수 있습니다. APK를 직접 설치할 때도 Quest 개발자 모드·USB 디버깅·ADB를 준비해야 합니다. EXE는 일반 사용자 권한으로 실행합니다. **연결 허용**을 누르면 방화벽 규칙을 확인하고, 변경이 필요할 때만 Windows 권한 요청 창을 표시합니다.

README의 앱 이미지는 실제 UI를 샘플 설정으로 PC에서 표시한 화면입니다. 헤드셋에서 촬영한 사진이나 AI 변환 결과는 아니며, 영상 수신·착용 검증을 대신하지 않습니다. 소개 이미지도 기능을 설명하기 위해 생성한 개념도입니다.

이 버전에서 확인한 내용과 아직 확인하지 못한 내용은 함께 배포하는 `release-validation.json`에 기록했습니다. 새 APK를 착용했을 때의 영상·소리, 영상과 소리의 시간차 측정, 장시간 사용은 아직 확인하지 못했습니다. 이전 버전의 기기 검증 결과를 이번 APK의 검증 결과로 취급하지 않습니다.

얇은 물체나 가려진 배경의 3D 윤곽 차이는 남아 있으며, 모든 장면에서 일정한 최소 FPS를 보장하지 않습니다. DRM이나 화면 캡처 차단이 적용된 콘텐츠도 정상 표시를 보장하지 않습니다. 이러한 제한은 기존 Preview 버전과 같습니다.
