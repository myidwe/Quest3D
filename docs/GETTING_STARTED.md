# 처음 설치와 연결

[English](GETTING_STARTED.en.md) · [문서 목차](README.md) · [프로젝트 소개](../README.md)

**0.1.3-preview** 설치 안내입니다. 처음 설치하고 연결을 승인한 뒤에는 **PC 시작 → Quest의 Connect**만 누르면 됩니다.

## 시작 전에

- **Windows x64 + NVIDIA Turing(sm75)** GPU가 필요합니다. Windows 11, RTX 2060 SUPER 8GB에서 개발하고 성능을 측정했습니다. 다른 Turing GPU의 메모리 사용량·인코더 동작·성능은 개별적으로 확인하지 않았으며, 다른 NVIDIA 세대와 AMD·Intel GPU는 현재 지원하지 않습니다.
- **Quest 2 또는 Quest 3**와 **16:9 모니터**가 필요합니다. PC와 Quest는 같은 공유기의 **신뢰할 수 있는 로컬 네트워크**에 연결합니다. USB는 앱 설치용이며 영상은 네트워크로 전송합니다.
- 처음 PC 앱을 설치할 때는 인터넷에 연결해야 하며, 실행 환경·GPU 라이브러리·AI 모델 등 수 GB 분량의 파일을 내려받습니다. 이후 AI 처리는 PC에서 실행합니다.

Windows 설치 파일에는 아직 코드 서명이 없어 SmartScreen 경고가 뜰 수 있습니다. 회사나 학교에서 관리하는 PC는 보안 정책에 따라 실행이 차단될 수도 있습니다. [설치 조건](EXE_INSTALLERS.md#권한과-windows-조건)과 [지원 범위](RELEASE_0.1.3_PREVIEW.md)를 확인하세요.

## 1. Windows 앱 설치

**[Desktop Setup EXE 다운로드](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Desktop-Setup-0.1.3-preview.exe)**

1. EXE를 실행하고 설치 폴더를 선택한 뒤 **설치**를 누릅니다. 다운로드와 설치가 끝날 때까지 기다립니다.
2. **연결 허용**을 누릅니다. Windows 권한 요청 창이 뜨면 내용을 확인하고 승인합니다.
3. **설치 창을 닫고** 바탕화면이나 시작 메뉴의 **Sterevi Desktop**을 엽니다.
4. **Settings → Quality → Headset**에서 사용할 Quest 2 / Quest 3를 선택합니다. 송출할 모니터도 확인합니다.

## 2. Quest 앱 설치

**[Quest Setup EXE 다운로드](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-Setup-0.1.3-preview.exe)**

Quest 앱을 설치하는 도구입니다. **Windows PC에서 실행**합니다.

1. [Meta 공식 안내](https://developers.meta.com/vr/documentation/native/android/mobile-device-setup/)에 따라 개발자 계정·팀에 필요한 조건을 확인합니다. Meta Horizon 앱에서 Quest의 **개발자 모드**를 켜고, Windows에 **Oculus ADB Drivers**를 설치합니다.
2. USB 데이터 케이블로 PC와 Quest를 연결하고, 헤드셋 안에서 **USB 디버깅 허용**을 승인합니다.
3. [Google 공식 Android Platform Tools](https://developer.android.com/tools/releases/platform-tools)를 내려받아 압축을 풉니다.
4. Quest Setup EXE를 실행하고 Platform Tools 폴더의 **adb.exe**를 선택합니다. **기기 검색 → 설치할 Quest 선택 → Quest에 설치** 순서로 진행합니다.
5. 헤드셋의 앱 목록에서 **알 수 없는 출처 → Sterevi**를 엽니다. 앱 목록 위치는 Horizon OS 버전에 따라 다를 수 있습니다.

기존 공개 앱을 사용 중이라면 앱을 삭제하지 않고 업데이트하세요. 이번 APK를 설치하면 앱 이름이 Sterevi로 바뀝니다. 패키지와 서명은 그대로이므로 기존 설정과 페어링 정보를 유지할 수 있습니다.

### APK 직접 설치

이미 사용 중인 APK 설치 도구가 있다면 [APK 파일](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-0.1.3-preview.apk)을 받아 직접 설치할 수 있습니다. 이때도 개발자 모드와 USB 디버깅 승인이 필요합니다. 공식 Platform Tools를 사용한다면 다음 명령으로 설치합니다.

```powershell
adb install -r "Sterevi-Quest-0.1.3-preview.apk"
```

`-r` 옵션은 기존 앱 데이터를 유지하면서 업데이트합니다. 처음 설치했다면 아래의 Pair/PIN 승인 절차를 진행하세요. 이미 연결한 PC에는 **Connect**로 접속합니다.

직접 내려받는 APK와 Quest Setup에 포함된 APK는 같은 파일입니다. 같은 릴리스의 `SHA256SUMS.txt`로 파일이 일치하는지 확인할 수 있습니다.

## 3. 처음 연결

1. PC와 Quest를 같은 공유기에 연결합니다. 집처럼 신뢰할 수 있는 네트워크에서 Windows 네트워크 프로필을 **개인**(Private)으로 설정합니다.
2. PC 앱에서 **PC 시작**을 누르고 송출 준비가 끝날 때까지 기다립니다.
3. Quest에서 **Select Server → Scan Network → 검색된 PC 선택 → Pair**를 누릅니다.
4. Quest에 표시된 네 자리 PIN을 PC 앱의 **Connection → 새 Quest 연결**에 입력하고 **연결 승인**을 누릅니다. 승인이 끝날 때까지 Quest에서 PIN 화면을 닫지 않습니다.
5. 자동으로 연결되지 않으면 Quest에서 **Connect**를 누릅니다.

PC가 검색되지 않으면 Quest의 **+** 버튼을 눌러 PC 앱에 표시된 주소를 입력합니다. 그래도 연결되지 않으면 [문제 해결](DESKTOP_USER_GUIDE.md#문제-해결)을 확인하세요.

## 다음부터 사용

**Sterevi Desktop 열기 → PC 시작 → Quest에서 기존 PC의 Connect**

연결 후 **Mode**에서 **2D / 3D**를 선택하고 **Depth**로 입체감을 조절합니다. 사용을 마칠 때는 PC 앱에서 **PC 중지**를 누릅니다. 자세한 화면·화질 설정은 [사용 방법](DESKTOP_USER_GUIDE.md)을 참고하세요.

소리는 기본적으로 **PC에서 재생**합니다. **Quest only**를 사용하려면 PC에 **Steam Streaming Speakers**가 설치되어 있고 활성화되어 있어야 합니다. [소리 출력 설정](DESKTOP_USER_GUIDE.md#sound--소리-출력)

업데이트·복구 방법과 설치 오류는 [상세 설치 안내](DISTRIBUTION.md)에 정리했습니다. [배포 안내](RELEASE_0.1.3_PREVIEW.md)에서는 지원 범위와 아직 확인하지 못한 내용을, [릴리스 페이지](https://github.com/myidwe/Sterevi/releases/tag/v0.1.3-preview)에서는 파일 해시와 검증 보고서를 확인할 수 있습니다.
