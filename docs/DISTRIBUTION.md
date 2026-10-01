# Quest3D 설치·배포 안내

**0.1.1-preview 설치 안내**. [같은 버전의 Release](https://github.com/myidwe/Quest3D/releases/tag/v0.1.1-preview)에서 Desktop ZIP과 Quest ZIP을 받는다. 기존 개발 앱·이전 검토 ZIP과 구분한다. 현재 지원 범위와 실제 검증 상태는 [배포 안내](RELEASE_0.1.1_PREVIEW.md), Release의 `release-validation.json`을 따른다.

## 받을 파일

GitHub **Releases**에서 같은 버전의 파일을 받는다. **Code → Download ZIP**은 개발용 소스이며 설치 파일이 아니다. [GitHub 공식 Release 안내](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

| 파일 | 용도 |
|---|---|
| `Quest3D-Desktop-<version>.zip` | Windows 앱과 설치 도구 |
| `Quest3D-Quest-<version>.zip` | Quest APK와 USB 설치 도구 |
| `Quest3D-Source-<version>.zip` | 해당 바이너리의 대응 소스·고지·빌드 자료 |
| `SHA256SUMS.txt` | 다운로드 파일의 SHA-256 확인 |
| `release-validation.json` | 실제 확인한 설치·동작과 미검증 범위 |
| `privacy-audit.json` | 최종 APK·ZIP의 개인정보 검사 결과 |

모델 가중치·개인 인증서·페어링·미디어·사용 로그는 ZIP에 넣지 않는다. 첫 PC 설치에서 고정 모델을 다운로드하고 이후 AI는 로컬에서 실행한다. 앱 구독·클라우드 추론료는 없다.

## 사용 조건

| 항목 | 현재 범위 |
|---|---|
| PC | Windows x64. 실제 제품 검증은 Windows 11, RTX 2060 SUPER 8 GB |
| GPU | NVIDIA Turing **sm75 / Compute Capability 7.5**만 허용. 다른 Turing GPU의 VRAM·NVENC·속도는 미검증 |
| 모니터 | 선택한 **16:9** 화면. 다른 비율은 시작 단계에서 거부 |
| 헤드셋 | Quest 2 / Quest 3. 기존 영상 연결·입체감 확인, 최신 APK의 Quest 2 재검증은 별도 |
| 연결 | 같은 사설 LAN. PC 유선·Quest 안정적인 Wi-Fi 권장. USB는 설치·진단용 |
| 설치 | 첫 다운로드 수 GB와 설치·캐시 공간 필요. 최소 RAM·디스크·드라이버 버전은 공개 후보 실측 전 미확정 |

AMD·Intel GPU, 다른 NVIDIA 아키텍처, ARM Windows, macOS/Linux 호스트는 현재 지원하지 않는다. RTX 30/40/50 계열도 CUDA 지원만으로 실행 가능하다고 판단하지 않는다. 설치기는 실제 CUDA 커널을 실행한다.

Quest 2는 눈별 **1920×1080**, Quest 3는 눈별 **2048×1152**가 PC 출력 프로필이다. Quest 3의 좌우 합친 **4096×1152 Full SBS**는 HEVC를 사용한다. 영상 전송 크기는 헤드셋 패널 해상도와 다르다. AI 갱신 FPS, 반복 송출 FPS, 헤드셋 표시율도 서로 다르다.

## PC 처음 설치

기존의 호환되는 Python 3.12.6(x64·Tk)이 있으면 재사용한다. 등록된 Python 3.12가 손상되었거나 버전이 다르면 자동 재설치를 차단하여 다른 프로그램의 환경을 보존한다. 이 경우 기존 환경을 복구하거나 호환되는 Python을 설치 명령의 `-Python`으로 지정한다. 오류 로그의 안내를 따르며 기존 Python 폴더를 삭제하지 않는다.

10월1일 새 호스트 후보는 개발 PC의 별도 A 드라이브 폴더에서 기존 Python3.12.6을 지정해 약3분54초에 설치했다. 고정 라이브러리·모델 새 다운로드, CUDA7종·Qt 화면을 통과했고 이후 실제 캡처·모델 추론·송출 시작도 확인했다. 이전 후보의 약10분7초와 조건이 같지는 않으며 설치 시간을 보장하지 않는다. 인터넷·디스크·기존 Python 상태에 따라 달라진다. 이전 설치·cache 명목 합계는 약10.7GiB였고 하드링크를 포함하므로 최소 공간 요구와 같지 않다. 업데이트 백업도 공간을 사용한다. Python 없는 새 Windows의 자동 설치·등록 검증은 남아 있다.

1. Desktop ZIP **전체**를 압축 해제한다. ZIP 안에서 설치 파일만 실행하지 않는다.
2. **Install-Quest3D.cmd**를 연다. 폴더와 바로가기를 확인하고 **설치**를 누른다. 기본은 `%LOCALAPPDATA%\Quest3D Desktop`이다. 공간이 부족하면 A 드라이브의 비어 있는 전용 폴더를 선택한다. 경로의 `#`, 따옴표, 줄바꿈은 지원하지 않는다.
3. Python **3.12.6 x64**, 고정 GPU 라이브러리, **Depth Anything V2 Small 약 99 MB**를 다운로드한다. **설치 로그**에서 진행·실패를 확인한다. CUDA·실제 앱 화면 검사까지 통과해야 설치 완료다.
4. **연결 허용**을 누른다. 버튼 안내에 허용 범위를 표시하며 별도 설명 확인창을 반복하지 않는다. 이미 올바른 규칙이면 관리자 요청도 생략한다. 변경이나 관리자 재확인이 필요한 경우 Windows 승인만 받는다. 사설망 로컬 서브넷의 앱 스트리밍 포트만 대상으로 하며 공용망·관리 페이지는 개방하지 않는다.
5. **앱 실행** 또는 **Quest3D Desktop** 바로가기를 연다. 송출 중지 상태에서 **Settings → Quality → Headset**의 Quest 2 / Quest 3를 선택하고 모니터를 확인한다.
6. **PC 시작**을 누르고 영상 준비를 기다린 뒤 Quest에서 Pair/Connect한다.

일상 사용에는 Codex·터미널·Unity·WSL·개발 도구가 필요 없다. 설치 도구는 Windows 자동 시작·서비스·브라우저 설정을 만들거나 바꾸지 않는다. 방화벽 변경은 별도 연결 허용 버튼으로 요청한다.

설치창은 실행 파일·설치 소유권을 확인하고 승격 자식의 종료 코드와 이번 작업의 새 결과를 추적한다. UAC 승인 자체를 방화벽 적용·실제 연결 성공으로 간주하지 않는다. 취소·실패·부분 적용은 미완료로 표시하며 기존 설정과 개인 데이터를 보존한다. 상태 조회 실패는 규칙 없음과 구분한다. 실제 적용·후속 상태 검사는 개발 PC에서 통과했다. 새 Windows의 다운로드 첫 실행·Python 공식 설치기·다른 관리자·사용자의 UAC 취소는 미검증이다. Python은 일반 사용자 설치를 요청하지만 시스템 C Runtime 보완의 관리자 예외가 있을 수 있다. 일상 앱 실행을 관리자 모드로 안내하지 않는다. [설정과 권한](SETUP_PERMISSIONS_2026-09-30.md)

연결 허용은 TCP `47984/47989/48010`, UDP `47998/47999/48000`이다. 자동 검색은 mDNS도 사용하며 개발 PC에서는 기존 Windows DNS Client의 사설망 UDP5353 허용을 이용했다. 다른 PC의 mDNS 정책·게스트 Wi-Fi 격리는 별도 검증 대상이다. Scan 실패 시 주소 입력을 사용할 수 있다.

## Quest 처음 설치

1. Meta 개발자 팀/계정 확인 요건을 충족하고 모바일 **Meta Horizon 앱 → 헤드셋 설정 → 개발자 모드**를 켠다. 현재 요건·메뉴는 [Meta 공식 기기 설정](https://developers.meta.com/vr/documentation/native/android/mobile-device-setup/)을 따른다.
2. Windows용 **Oculus ADB Drivers**를 준비한다. 공식 기기 설정의 링크·설치 절차를 사용한다. Quest3D 설치기가 드라이버를 자동 설치하지 않는다.
3. USB 데이터 케이블로 연결하고 헤드셋 안에서 **USB 디버깅 허용**을 확인한다.
4. [Google 공식 Android Platform Tools](https://developer.android.com/tools/releases/platform-tools)를 다운로드해 압축을 푼다. Android Studio 전체 설치는 필요하지 않다.
5. Quest ZIP 전체를 압축 해제하고 **Install-Quest.cmd**를 연다. `adb.exe`를 선택한 뒤 **기기 검색 → 설치할 Quest 선택 → Quest에 설치**를 누른다. 여러 기기가 연결됐으면 대상을 명시적으로 선택한다.
6. 헤드셋 앱 목록의 **알 수 없는 출처 → Quest 3D Desktop**을 연다. 앱 목록 위치·표기는 Horizon OS 버전에 따라 확인한다.

USB 디버깅·개발자 모드는 소유자가 직접 승인한다. 설치창의 **기기 검색**에서 Quest를 선택한다. 승인 대기·오프라인·다른 Android 기기를 구분하며 승인된 Quest에만 설치한다. 여러 헤드셋이 연결되어 있어도 설치 대상을 명시적으로 선택한다. 설치 도구는 APK 해시·기기 종류·버전 기록을 확인하고 `adb install -r`로 설치한다. 이전 versionCode로의 설치·기존 앱 자동 삭제·데이터 초기화는 하지 않는다.

USB 디버깅 승인은 해당 PC의 설치·디버깅 접근이며 앱 PIN 페어링·MTP 파일 접근과 별개다. 최신 개발 APK manifest에는 마이크·카메라·외부 저장소·Scene 권한이 없다. PC 소리 수신 때문에 마이크 승인을 요구하지 않는다. 실제 새 설치의 최초 실행 시스템 안내는 별도 검증한다. 현재 PC/Quest 설치창은 진행 중 취소를 제공하지 않으므로 완료/실패를 기다린 후 재시도한다.

기존 개발 APK는 `app.questto3d.client.debug`다. 공개 앱은 **`app.questto3d.client`**로 별도 설치하며 기존 개발 앱을 삭제하지 않는다. 새 공개 앱에서 PC를 다시 검색·페어링한다. 이후 공개 앱 업데이트는 같은 패키지·공개 서명키를 유지하고 versionCode를 높여 설치한다. 개발 앱의 설정·페어링을 공개 앱에 자동 이전하지 않는다.

## 첫 연결과 매번 사용

**PC 앱 → PC 시작 → Quest 앱 Connect**가 일상 순서다.

새 헤드셋은 **Select Server → Scan Network → 검색된 PC → Pair**로 연결한다. 검색되지 않으면 **+**에 PC 앱 주소를 입력한다. 헤드셋의 네 자리 PIN을 PC 앱 **Connection → 새 Quest 연결**에 입력하고 승인한다. 승인까지 Quest PIN 화면을 유지하고, 필요하면 Connect를 누른다. 각 PC·헤드셋은 새로 페어링한다.

2D/3D·Depth·윤곽 안정화를 조절한다. Quest Display에서는 크기·거리·위치·곡률·여백·보기 저장을, Quality에서는 선명도·색감·전송을 조절한다. 최신 설정창은 세로 구성이며 기본 Level은 수평을 유지하고 Free를 명시적으로 선택하면 기울기를 조절한다. 자세한 동작은 [사용 안내](DESKTOP_USER_GUIDE.md)를 따른다.

창 숨기기는 송출 유지, **PC 중지 / 중지 후 종료**는 정상 종료다. Quest의 Windows 클릭·드래그·스크롤, 내장 영상·사진 플레이어, 원래 화면 안 선택 영역 입체화는 현재 제품 범위에 포함하지 않는다.

## Sound · 소리 출력

**PC 중지 → Settings → Sound → 출력 선택 → PC 시작 → Quest Connect** 순서로 적용한다.

| 선택 | 동작·조건 |
|---|---|
| PC · 기본 | 기존 PC 스피커·헤드폰. Quest 오디오 전송은 끔 |
| PC + Quest | PC 기본 장치의 소리와 Quest 동시 재생. 두 소리가 겹칠 수 있음 |
| Quest only | 연결 중 기존 가상 출력으로 라우팅해 Quest 재생. 연결 종료·PC 중지 시 PC 출력 복원 |

Quest only는 **이미 설치·활성화된 Steam Streaming Speakers**가 필요하다. ZIP에는 이 드라이버가 없고 자동 설치하지 않는다. 새 PC에 없으면 이유와 함께 해당 선택을 비활성화한다. 공개 드라이버 설치·재배포 경로는 추가 검토 대상이며 필수 설치 단계로 취급하지 않는다.

Quest 음량은 헤드셋 볼륨 버튼으로 조절한다. 특정 앱이 출력 장치를 고정했다면 Windows 음량 믹서에서 **기본값**으로 바꾸고 재생을 다시 시작한다. 앱별 출력을 강제로 변경하지 않는다. 실제 수신·장치 복원은 개발 PC/Quest 3에서 확인했지만 착용 청취·영상 대비 음성 오차·장시간·최신 Quest 2 검증은 남아 있다. 지연 조절 기능이 완성됐다고 표시하지 않는다.

## AI Quality와 선택 모델

기본 모델은 **DAv2 Small**이다. **Settings → Quality → AI Quality**에서 Standard / Quality · Preview를 선택하고 PC를 중지·재시작한다. AI 분석 해상도가 바뀌며 송출 해상도는 유지한다. Preview는 더 많은 픽셀을 분석하지만 모든 장면의 윤곽·입체감 개선을 보장하지 않는다.

**DAD Small**은 비교용이며 기본 설치에는 없다. 현재 추가 설치는 앱을 중지하고 설치 폴더에서 실행하는 고급 절차다. GUI 다운로드 기능은 없다.

```powershell
.\.venv\Scripts\python.exe -m quest3d.cli setup-model --model-id distill_any_depth_small
```

약 99 MB를 다운로드하며 이후 **Settings → Quality → Depth model**에서 선택한다. 설치된 모델은 송출 중에도 전환 가능하며 미설치 항목은 비활성이다. 고정 revision·SHA-256·라이선스는 `config/models.json`과 [제3자 고지](../THIRD_PARTY_NOTICES.md)에 기록한다. 추론 중에는 다운로드하지 않는다.

## 업데이트·제거

PC를 중지하고 앱을 종료한 뒤 **새 Desktop ZIP 전체 압축 해제 → Install-Quest3D.cmd → 기존 설치 폴더 선택 → 업데이트** 순서로 진행한다. 앱 소유 파일만 갱신하고 설정·페어링·모델·사용자 파일을 보존한다. 이전 앱과 venv는 transaction 백업에 남기며 설치 검사 실패나 중단 후 재실행 시 원복한다. 업데이트 완료 후에도 설치 관리창의 **이전 버전 복원**을 사용할 수 있다.

Python 버전 또는 host 해시·경로가 바뀌는 비호환 업데이트는 현재 차단한다. 이 경우 별도 설치·마이그레이션 안내가 필요하다. 수정된 앱 파일·알 수 없는 파일 충돌·변경된 백업·실행 중인 앱은 덮어쓰지 않는다. 원복 중 수동 변경을 발견하면 보존하고 오류를 알려준다. 검증된 앱 소유 바로가기는 교체할 수 있으며 다른 바로가기는 보존한다.

공개 ZIP의 installer에는 A 드라이브 등 선택한 설치 폴더 아래 `.cache/uv`를 사용하도록 반영했다. 첫 의존성 다운로드 캐시가 별도 C 드라이브 기본 cache에 쌓이는 것을 줄인다. 설치 검증 결과와 로그는 로컬에 남으며 업로드하지 않는다.

Quest APK는 같은 패키지·서명 업데이트만 데이터를 보존해 설치할 수 있다. 서명 오류 시 강제 제거하지 말고 출처·마이그레이션 안내를 확인한다.

PC 제거는 **PC 중지 후 종료 → 설치 폴더의 Install-Quest3D.cmd → 제거·복구 보관**을 사용한다. 앱 소유 방화벽이 있으면 그 정리만 관리자 권한으로 진행한다. 명확히 없는 경우에는 재조회 후 관리자 요청을 생략한다. 승인을 취소하거나 정리가 실패하면 앱을 보존한다. 바로가기와 파일 보관은 원래 Windows 사용자 권한으로 처리한다. 검증된 설치 폴더를 같은 상위 폴더의 `.Quest3D-removed-<ID>`로 보관하고 정확히 일치하는 앱 소유 바로가기만 제거한다. **이 보관 제거는 디스크 공간을 회수하지 않는다.** 설정·모델·페어링을 남기는 안전한 retirement이며 무조건 폴더를 재귀 삭제하는 기능이 아니다. 백업을 직접 복구하려면 원래 설치 경로로 되돌린 뒤 설치 관리/바로가기를 확인한다. Python 공식 설치기가 따로 등록한 앱 전용 Python은 Windows 설치된 앱에서 확인해 제거하며 다른 프로그램이 쓰는 Python은 보존한다. Quest 앱 제거는 앱 데이터·페어링·보기 설정도 삭제하므로 업데이트와 구분한다.

## 오류와 제한

| 증상 | 확인 |
|---|---|
| 다운로드 실패 | 인터넷·디스크·설치 로그. 같은 묶음으로 재시도 |
| Python 준비 실패 | 정확한 3.12.6 x64 + Tk 충돌 여부. 고급 `install.ps1 -Python <경로>` 가능 |
| CUDA 검사 실패 | Turing sm75·드라이버·로그. 다른 아키텍처/CPU 대체 없음 |
| 앱 화면 검사 실패 | 전체 ZIP 재추출·Qt/QML/자원 로그. 설치 미완료 |
| PC 검색·연결 실패 | PC 시작·사설 LAN·연결 허용·mDNS·게스트 격리. 주소 입력으로 구분 |
| USB 기기 없음 | 데이터 케이블·개발자 모드·ADB 드라이버·디버깅 승인 |
| APK 서명 오류 | 기존 앱과 새 APK의 패키지/서명. 자동 제거하지 않음 |
| PC 시작 실패 | 16:9 모니터·다른 Sunshine의 포트 점유·진단 |
| Quest only 비활성 | Steam Streaming Speakers 존재·활성·음량과 PC 기본 출력 |
| 3D 윤곽 겹침 | Depth를 낮추고 윤곽 안정화 비교. 불편하면 2D |

보호 콘텐츠는 검은 영상·캡처 차단이 생길 수 있다. 개발 PC는 브라우저 하드웨어 가속 해제로 정상 영상이 보였다는 사용자 확인이 있다. 모든 서비스·작품·브라우저 버전 지원이나 DRM 해제를 뜻하지 않는다.

개발 PC의 최근 DAD Standard / Quality 수치는 **새 3D 결과 약 38.66 / 34.64개/s**인 PC 처리 경로다. 60회/s 반복 게시, Quest 실제 수신 FPS, 종단 지연·음성 동기화를 같은 수치로 표시하지 않는다. 얇은 사물·가려진 배경의 윤곽 한계도 남아 있다.

## 진단과 공개 전 확인

설치 로그는 설치창에서, 사용 진단은 PC 앱에서 연다. PC 주소·경로·장치 정보가 들어갈 수 있으므로 검토한 뒤 공유한다. `config/artifacts`에는 개인 설정·페어링·실행 정보가 생긴다. **설치 폴더 전체를 업로드하거나 전달하지 않는다.**

이전 2026-09-11 후보는 같은 PC의 새 폴더·새 venv·실제 PC 시작/중지를 확인했지만 Python 공식 설치기 자체, 새 host의 Quest 페어링·영상, 다른 물리 PC는 확인하지 않았다. 이후 DAD·Quest 3 검색·AI Quality·화면 설정·소리가 추가됐다. 이전 ZIP/검증을 최신 공개판 검증으로 재사용하지 않는다.

최신 후보는 무결성·고지·대응 소스와 함께 **새 설치 → 바로가기 → 사설망 → Scan/새 PIN → 실제 양안 → 설정 보존 → 정상 종료·소리 복원 → 업데이트/제거**를 확인하고 `release-validation.json`에 남긴다. 빌드와 남은 재현 제한은 [BUILDING](BUILDING.md)을 따른다.
