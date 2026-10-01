# Quest3D 0.1.0-preview

Windows 화면을 Quest에서 보고 로컬 AI로 2D/스테레오 3D를 전환하는 첫 공개 Preview다. **Windows x64 / NVIDIA Turing sm75 / 16:9 모니터**로 지원 범위를 제한한다. 실측 GPU는 RTX 2060 SUPER 8 GB다. 다른 GPU 세대는 현재 지원하지 않는다.

## 설치

[Release](https://github.com/myidwe/Quest3D/releases/tag/v0.1.0-preview)에서 같은 버전의 Desktop ZIP과 Quest ZIP을 받는다.

1. Desktop ZIP 전체 압축 해제 → **Install-Quest3D.cmd → 설치 → 연결 허용**
2. Quest 개발자 모드·USB 디버깅 승인 → Google Platform Tools 준비 → **Install-Quest.cmd**
3. 같은 사설망 → **Quest3D Desktop → PC 시작**
4. Quest **Scan Network → PC → Pair** → PC 앱에서 PIN 승인 → **Connect**
5. 다음부터 **PC 시작 → Quest Connect**

처음 PC 설치에는 수 GB의 고정 라이브러리·모델 다운로드가 필요하다. 설치 폴더 아래에 다운로드 cache를 사용하며 A 드라이브 등 원하는 빈 폴더를 선택할 수 있다. 일상 실행에는 Codex·WSL·터미널·개발 도구가 필요 없다. [설치·업데이트·복구](DISTRIBUTION.md) · [사용 안내](DESKTOP_USER_GUIDE.md)

## 공개 빌드

새 호스트와 전체 Quest APK를 실제 소스로 빌드했다. 배포 binary·대응 source·라이선스 고지·package/version/서명의 관계를 파일 해시로 대조한다. APK 빌드 중 발견한 AAR의 이전 native 우선 선택은 수정했고, 잘못된 첫 APK는 서명·배포하지 않았다. OpenXR vendor는 고정 Khronos 공개 헤더를 사용한다. 사용하지 않는 Meta preview SDK/header 바이트는 대응 소스에 포함하지 않는다.

공개 Quest 패키지는 **`app.questto3d.client`**, versionCode **1**이다. 기존 `app.questto3d.client.debug` 앱을 삭제하지 않는다. 새 공개 앱에서 다시 Pair한다. 앞으로 같은 공개 패키지·서명키와 더 높은 versionCode로 데이터를 보존해 업데이트한다.

APK 공개 서명 지문(SHA-256):

```text
d950d11633753a3a52acba925dff8a35ccc0df7de1aef77291a1868c93f73cfc
```

Source ZIP은 완전한 native 수정·의존성 입력·고지·재현 도구를 제공한다. GitHub 자동 Source code ZIP은 이를 대신하지 않는다. 서명 개인키·페어링·설정·미디어·모델 가중치는 배포하지 않는다. [빌드](BUILDING.md) · [제3자 고지](../THIRD_PARTY_NOTICES.md)

## 확인한 범위

개발 PC의 별도 A 드라이브 새 설치에서 모델 다운로드·실제 CUDA·Qt/QML 검증을 통과했다. 실제 모니터 캡처·로컬 AI·새 호스트 시작, 2D/3D 제어 ACK, 정상 종료·재시작을 확인했다. 같은 host 버전 업데이트에서 설정·모델·인증서가 보존됐다. 새 호스트의 실제 loopback·H.264/HEVC NVENC 초기화·중복 실행 거부도 통과했다.

Windows 방화벽의 Description `|` 금지로 발생한 실제 오류를 수정했다. 수정 후보에서 관리자 실행 결과와 새 상태 조회·독립 OS 규칙 대조를 확인했다. exact 앱 / Private / LocalSubnet의 스트리밍 TCP·UDP만 허용하며 공용망·관리 페이지47990·검색5353·기존 개발 규칙은 변경하지 않는다. 일반 실행에 관리자 권한은 필요 없다.

**최종 파일의 설치·업데이트·Quest 결과와 정확한 검사 수는 Release의 `release-validation.json`을 따른다.** 이전 개발 앱 착용 결과, 같은 PC의 격리 설치, 작은 CUDA 검사, native 초기화를 서로 대신하는 증거로 쓰지 않는다.

## 남은 제한

- Python 없는 새 Windows, 다른 PC/GPU, 다른 관리자 계정의 UAC·OS 정책
- 새 공개 APK의 Quest 2 재검증, 사용자 청취·AV 오차·장시간 안정성
- 장면별 새 3D FPS·윤곽 품질 차이, 얇은 물체·가려진 배경의 잔여 오차
- 사설망 mDNS 정책·공유기 격리 차이. Scan 실패 시 주소 입력으로 연결
- DRM·캡처 차단 콘텐츠. 모든 서비스·브라우저 재생을 보장하지 않음

Quest 2는 눈별1920×1080, Quest 3는2048×1152다. Quest 3의 Full SBS4096×1152는 HEVC를 사용한다. 새 AI 프레임, 반복 송출, Quest 수신, 헤드셋 표시율은 다르다. 모든 장면의 최소30FPS나 새3D60FPS를 약속하지 않는다.

소리는 PC 기본이다. **PC + Quest**는 동시 출력이며 **Quest only**는 기존 활성 Steam Streaming Speakers가 필요하다. 해당 드라이버는 배포·자동 설치하지 않는다. Quest PC 입력·내장 미디어 플레이어·선택 영역만 입체화·별도 AV 보정은 현재 범위에 없다.
