# 0.1.3-preview · Sterevi

**[처음 설치와 연결](GETTING_STARTED.md)** · [사용 방법](DESKTOP_USER_GUIDE.md) · [문서 목차](README.md)

공개 제품 이름을 **Sterevi · 스테레비**로 바꾸는 업데이트다. PC 앱·설치창·바로가기, Quest 앱 목록·시작·설정 제목, GitHub 안내에 반영한다. 캡처·AI·좌우 합성·코덱·오디오·모델·기본 화질과 성능 설정은 유지한다.

## 다운로드

| Windows | Quest |
|:---|:---|
| [Desktop Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Desktop-Setup-0.1.3-preview.exe) | [Quest Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-Setup-0.1.3-preview.exe) |
| PC 앱 설치·업데이트 | Windows에서 USB 설치 |
| | [APK 직접 다운로드](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-0.1.3-preview.apk) · 기존 설치 도구로 직접 설치 |

수동 설치 ZIP·전체 대응 Source ZIP·`SHA256SUMS.txt`·개인정보 검사·실제 검증 범위는 [같은 Release](https://github.com/myidwe/Sterevi/releases/tag/v0.1.3-preview)에 제공한다. APK 직접 파일은 Quest Setup에 들어 있는 APK와 같은 바이트다. GitHub의 자동 Source code ZIP은 전체 네이티브 대응 Source ZIP을 대신하지 않는다.

## 기존 사용자 업데이트

- PC 설치창에서 기존 설치 폴더를 확인하고 **업데이트**를 선택한다. 이름이 바뀌었다는 이유로 모델을 지우거나 설정·페어링을 초기화하지 않는다. 이후 **Sterevi Desktop → PC 시작**으로 사용한다.
- Quest APK는 **0.1.3-preview / versionCode 4**, 공개 패키지는 기존 **`app.questto3d.client`**, 서명 인증서는 기존 공개 앱과 같다. EXE 또는 `adb install -r`로 업데이트한다. 앱을 먼저 삭제하지 않는다.
- 기존 공개 앱의 설정·보기 저장·PC 페어링은 유지한다. 새로 설치한 사용자는 첫 Pair/PIN 승인이 필요하다. 이미 저장된 PC에는 Connect를 사용한다.
- 기술적인 Python 모듈·설치 소유권·통신 이름·사용자 데이터 경로에는 이전 이름이 남을 수 있다. 데이터 보존과 중복 실행 방지를 위한 호환 정보이며, 사용자에게 보이는 제품 이름은 Sterevi다.
- 이전 릴리스의 파일명·버전·태그는 당시 배포 기록으로 보존한다.

## 지원과 검증 범위

Windows x64, NVIDIA Turing(sm75), Quest 2 / 3, 16:9 모니터, 같은 신뢰하는 사설 LAN을 대상으로 한다. 실제 개발·검증 PC는 RTX 2060 SUPER 8GB / Windows 11이다. 다른 Turing 모델의 메모리·인코더·성능, 다른 PC의 설치 환경은 개별 검증하지 않았다. 다른 NVIDIA 세대·AMD·Intel GPU는 현재 지원하지 않는다.

Windows EXE에는 신뢰 코드 서명이 없어 SmartScreen·관리 PC 정책에서 경고나 차단이 생길 수 있다. Quest 개발자 모드·USB 디버깅·ADB 준비는 직접 APK 설치에서도 필요하다. EXE 자체는 일반 사용자 권한이며, 연결 허용 버튼은 필요한 방화벽 변경에 Windows 승인을 요청한다.

README의 앱 사진은 생산 UI를 개인정보 없는 샘플 상태로 렌더한 캡처다. 실제 헤드셋 사진·실제 AI 변환 결과·영상 수신·착용 검증을 대신하지 않는다. 생성한 소개 이미지도 기능 개념도다.

이 릴리스의 실제 통과 항목과 아직 확인하지 않은 실기·정량 AV·장시간 항목은 함께 제공하는 `release-validation.json`에 기록한다. 이전 장비 검증을 이번 새 APK의 검증으로 보고하지 않는다. 얇은 물체·가려진 배경의 입체 윤곽 차이, 모든 장면의 최소 FPS, DRM·캡처 차단의 제한은 기존 Preview 범위를 유지한다.
