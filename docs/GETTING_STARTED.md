# 처음 설치와 연결

[English](GETTING_STARTED.en.md) · [문서 목차](README.md) · [프로젝트 소개](../README.md)

**0.1.3-preview** 기준입니다. 처음 설치를 마치면 매번 **PC 시작 → Quest Connect**로 사용합니다.

## 시작 전에

- **Windows x64 + NVIDIA Turing(sm75)** GPU가 필요합니다. 실측 장비는 Windows 11, RTX 2060 SUPER 8GB입니다. 다른 Turing 모델의 메모리·인코더·성능은 개별 검증하지 않았으며, 다른 NVIDIA 세대·AMD·Intel GPU는 현재 지원하지 않습니다.
- **Quest 2 또는 Quest 3**, **16:9 모니터**, PC와 Quest가 함께 사용할 **사설 LAN**을 준비합니다. USB는 Quest 앱 설치용입니다.
- 처음 PC 설치는 인터넷이 필요하며 고정 실행 환경·GPU 라이브러리·모델을 수 GB 다운로드합니다. 이후 AI 처리는 PC에서 실행합니다.

Windows EXE에는 아직 신뢰 코드 서명이 없어 SmartScreen 또는 관리 PC 정책에서 경고·차단이 생길 수 있습니다. [EXE 설치 조건](EXE_INSTALLERS.md#권한과-windows-조건)과 [실제 검증 범위](RELEASE_0.1.3_PREVIEW.md)를 확인하세요.

## 1. Windows 앱 설치

**[Desktop Setup EXE 다운로드](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Desktop-Setup-0.1.3-preview.exe)**

1. EXE를 열고 설치 폴더를 선택한 뒤 **설치**를 누릅니다. 다운로드와 설치가 끝날 때까지 기다립니다.
2. 설치 완료 후 **연결 허용**을 누릅니다. Windows 승인이 표시되면 확인합니다.
3. **설치창을 닫고**, 바탕화면이나 시작 메뉴의 **Sterevi Desktop**을 엽니다.
4. 송출을 시작하기 전에 **Settings → Quality → Headset**에서 사용할 Quest 2 / Quest 3를 선택하고 모니터를 확인합니다.

## 2. Quest 앱 설치

**[Quest Setup EXE 다운로드](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-Setup-0.1.3-preview.exe)** — 이 파일도 Windows PC에서 실행합니다.

1. [Meta 공식 기기 설정](https://developers.meta.com/vr/documentation/native/android/mobile-device-setup/)에 따라 개발자 계정·팀 요건을 확인하고 Meta Horizon 앱에서 Quest의 **개발자 모드**를 켭니다. 같은 공식 안내에 따라 Windows용 **Oculus ADB Drivers**를 설치합니다.
2. USB 데이터 케이블로 PC와 Quest를 연결하고, 헤드셋 안에서 **USB 디버깅 허용**을 승인합니다.
3. [Google 공식 Android Platform Tools](https://developer.android.com/tools/releases/platform-tools)를 받아 압축을 풉니다.
4. Quest Setup EXE를 열고 Platform Tools의 **adb.exe**를 선택합니다. **기기 검색 → 설치할 Quest 선택 → Quest에 설치**를 누릅니다.
5. 헤드셋의 앱 목록에서 **알 수 없는 출처 → Sterevi**를 엽니다. 앱 목록 위치는 Horizon OS 버전에 따라 다를 수 있습니다.

기존 공개 앱은 같은 패키지·서명으로 업데이트됩니다. 이름을 Sterevi로 바꾸려면 이번 APK를 설치하세요. 기존 설정과 페어링은 유지되며 삭제 후 재설치는 필요하지 않습니다.

### APK 직접 설치

기존 설치 도구를 사용한다면 EXE 옆의 [APK 직접 다운로드](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-0.1.3-preview.apk)를 선택하세요. 개발자 모드와 USB 디버깅 승인은 동일하게 필요합니다. 공식 Platform Tools로 직접 설치할 때는:

```powershell
adb install -r "Sterevi-Quest-0.1.3-preview.apk"
```

`-r`은 기존 공개 앱의 데이터를 유지하는 업데이트입니다. 처음 설치는 아래 Pair/PIN 연결이 필요하고, 기존 PC는 Connect로 연결합니다. EXE에 들어 있는 APK와 같은 파일이며, 같은 Release의 `SHA256SUMS.txt`로 확인할 수 있습니다.

## 3. 처음 연결

1. PC와 Quest를 같은 공유기의 사설 LAN에 연결합니다. 신뢰하는 집 네트워크에서 Windows 네트워크 프로필이 **개인**(Private)으로 설정됐는지 확인합니다.
2. PC 앱에서 **PC 시작**을 누르고 영상 준비를 기다립니다.
3. Quest에서 **Select Server → Scan Network → 검색된 PC 선택 → Pair**를 누릅니다.
4. Quest에 표시된 네 자리 PIN을 PC 앱의 **Connection → 새 Quest 연결**에 입력하고 **연결 승인**을 누릅니다. 승인될 때까지 Quest의 PIN 화면을 유지합니다.
5. 자동으로 연결되지 않으면 Quest에서 **Connect**를 누릅니다.

PC 검색이 안 되면 Quest의 **+** 버튼에서 PC 앱에 표시된 주소를 입력할 수 있습니다. 연결이 계속 실패하면 [연결 문제 해결](DESKTOP_USER_GUIDE.md#문제-해결)을 확인하세요.

## 다음부터 사용

**Sterevi Desktop 열기 → PC 시작 → Quest에서 기존 PC의 Connect**

연결 후 **Mode · 2D / 3D**와 **Depth**로 입체감을 조절합니다. 종료할 때는 PC 앱에서 **PC 중지**를 누릅니다. [화질·화면 조절과 종료](DESKTOP_USER_GUIDE.md)를 참고하세요.

소리는 **PC 출력이 기본**입니다. Quest only는 이미 설치·활성화된 **Steam Streaming Speakers**가 필요합니다. [소리 출력 선택](DESKTOP_USER_GUIDE.md#sound--소리-출력)

업데이트·복구·설치 오류는 [상세 설치 안내](DISTRIBUTION.md), 지원 범위와 남은 검증은 [현재 배포 안내](RELEASE_0.1.3_PREVIEW.md), 파일 해시와 검증 보고서는 [현재 Release](https://github.com/myidwe/Sterevi/releases/tag/v0.1.3-preview)에서 확인할 수 있습니다.
