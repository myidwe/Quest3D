# 설치·연결 설정과 권한 감사

상태: **2026-10-01 보완 구현·실제 적용 검증**. 아래의 기존 권한 조사는 읽기 전용이었고 이후 새 공개 PC 후보에서는 별도 방화벽 적용을 검증했다. 네트워크 프로필·실행 정책과 기존 개발 설치는 변경하지 않았다. 이전 d ZIP 및625파일 Git 후보는 보존한다.

## 10월 1일 공개 PC 후보의 실제 적용

이 아래의 기존 조사와 별도로 새 호스트 `86eb2ee5…`의 A 드라이브 격리 설치에서 연결 허용 흐름을 실제 실행했다. 사용자 설치 소유권·manifest·exe 확인 → 새 작업 ID → Windows 승격 실행 → 적용 receipt → 새 상태 조회를 통과했다. **새 설치본 전용 TCP/UDP 규칙 2개**가 exact Program, Inbound Allow, Private, LocalSubnet, EdgeTraversal Block 및 아래 스트리밍 포트와 일치했다. 기존 개발 규칙·네트워크 프로필·관리 페이지47990·검색5353 정책은 변경하지 않았다.

첫 시도는 Windows가 규칙 Description의 `|` 문자를 금지해 실패했다. 생성·삭제·부분 적용·rollback error는 모두 0이며 실패 기록을 보존했다. 설명을 짧은 설치 식별자로 고치고 실제 Program filter·path 기반 Name/Group·scope 검증은 유지했다. 수정 후 적용·독립 실제 규칙 조회까지 통과했다. [Microsoft Description 제약](https://learn.microsoft.com/en-us/windows/win32/api/netfw/nn-netfw-inetfwrule)

승격 자식의 실제 결과를 확인했지만 사용자에게 UAC가 보인 방식·취소·다른 관리자 계정·새 Windows를 모두 검증한 것은 아니다. 실제 적용 성공과 이 미검증 조건을 구분한다. 아래 4개 규칙은 기존 개발 설치의 조사 결과다.

## 현재 PC의 방화벽

기존 개발 설치 조사 당시 네트워크는 Private이고 Domain/Private/Public의 Windows 방화벽은 모두 켜져 있었다. Quest2·Quest3용 허용 규칙4개의 프로그램 경로는 기존 host와 일치했고 해당 host SHA256은 `77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63`다.

| 범위 | 현재 개발 PC | 배포 설치창의 구현 |
|---|---|---|
| 프로그램 | 검증한 기존 sunshine.exe | 해당 설치 폴더의 고정 sunshine.exe |
| 네트워크 | Private | Private |
| 상대 주소 | Quest2·Quest3의 지정 IPv4 각각 | LocalSubnet |
| TCP | 47984 / 47989 / 48010 | 동일 |
| UDP | 47998 / 47999 / 48000 | 동일 |
| 관리 페이지47990 | 해당 허용 규칙에 포함하지 않음 | 포함하지 않음 |
| 자동 검색5353 | 기존 Windows DNS Client 경로 | 새 host 방화벽 규칙에는 추가하지 않음 |

현재 특정 기기 IP를 허용한 개발 규칙과 일반 사용자의 LocalSubnet 설치 규칙은 같지 않다. 배포 경로는 DHCP로 Quest 주소가 바뀌어도 사용할 수 있도록 사설망 로컬 서브넷 범위를 안내하고 사용자가 연결 허용을 선택한다. Private와 LocalSubnet은 [Microsoft 규칙 매개변수](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule?view=windowsserver2025-ps)를 사용한다. 이 값이 해당 서브넷의 기기 신원을 인증하는 것은 아니며 앱의 PIN 페어링은 별도다.

현재 확인은 Quest 규칙4개와 프로필·프로그램의 대조다. 전체5353 필터를 읽는 과정에 접근 거부가 일부 있었으므로 모든 유효 정책·조직 정책·다른 프로그램의 규칙을 감사했다고 주장하지 않는다. 공유기 격리·다른 PC의 검색 성공을 다시 검증한 것도 아니다.

## 사용자가 요청받는 설정과 승인

| 시점 | 요청·조건 | 현재 동작과 사용자 행동 |
|---|---|---|
| PC 설치 | 쓰기 가능한 전용 폴더·인터넷·설치 공간 | 일반 사용자 설치. 폴더·바로가기를 선택하고 Python/고정 라이브러리/모델 준비 시작 |
| Python 최초 준비 | 드문 시스템 런타임 보완 | per-user 설치 요청. 시스템 C Runtime 보완의 관리자 예외와 새 Windows 설치는 미검증 |
| PC 연결 허용 | Windows 관리자 승인 | 범위 안내·버튼 → 읽기 상태 확인 → 변경이 필요한 경우만 UAC → 실제 결과 확인 |
| Windows 네트워크 | 신뢰하는 사설 LAN | 현재 프로필 확인. Public이면 사용자가 신뢰하는 네트워크에 한해 Windows 설정에서 Private 선택 |
| Quest 초기 설정 | Meta 개발자 팀/계정 확인·개발자 모드 | 사용자가 Meta 공식 화면에서 처리. 우리 앱이 계정·인증 정보나 결제 정보를 받지 않음 |
| Quest USB 설치 | 데이터 케이블·ADB 드라이버·USB 디버깅 RSA 승인 | 본인 PC의 연결을 헤드셋에서 승인 → 설치창에서 기기 검색·명시적 Quest 선택 |
| 최초 앱 연결 | Quest의4자리 PIN | PC Connection에서 해당 새 요청 승인. USB 승인과 별개의 앱 연결 |
| Quest only 소리 | 기존 활성 Steam Streaming Speakers | 선택 기능의 외부 장치 조건. 우리 설치기가 드라이버를 자동 설치·배포하지 않음 |
| 앱 방화벽 제거 | 관리자 승인 | 필요한 앱 방화벽 정리만 승격. 규칙이 명확히 없으면 생략. 파일·바로가기는 원사용자 권한 |

기본 폴더는 사용자 `%LOCALAPPDATA%`이며 사용자가 쓸 수 있는 A 드라이브의 전용 폴더도 선택할 수 있다. 보호된 시스템 폴더 설치를 위해 앱 전체를 관리자 모드로 실행하는 흐름을 기본값으로 삼지 않는다. Python은 `InstallAllUsers=0`, PATH·전역 launcher·파일 연결·Python 바로가기 비활성으로 실행하며 HKCU 등록이 생길 수 있다. per-user에서도 시스템 런타임 보완 예외가 있는 것은 [Python 공식 설명](https://docs.python.org/3.12/using/windows.html#installation-steps)을 따른다. 기존 검증은 설치된 Python 재사용이며 이 예외를 실측하지 않았다.

Private 전환은 사용자 판단이 필요한 설정이다. 신뢰하는 사람·기기의 네트워크에서 선택하도록 안내하며 앱이 자동으로 프로필을 바꾸지 않는다. [Windows 네트워크 프로필 안내](https://support.microsoft.com/en-us/windows/experience/connectivity-networking/essential-network-settings-and-tasks-in-windows)

Quest 개발자 팀·계정 확인·개발자 모드와 Windows ADB 드라이버는 [Meta 공식 기기 설정](https://developers.meta.com/vr/documentation/native/android/mobile-device-setup/)에 연결한다. USB RSA 승인은 해당 PC의 ADB 설치·디버깅 접근을 허용하는 것이며 MTP 파일 접근, PC 앱의 PIN 연결, Windows UAC와 구분한다. [Google ADB 안내](https://developer.android.com/tools/adb) 승인되지 않은 기기를 설치기가 자동 승인하거나 앱 데이터를 지우지 않는다. 최초 설치 이후 영상 연결은 LAN이며 USB 케이블은 일상 시청의 필수 조건이 아니다.

## 실제 Quest APK의 권한

최신 개발 APK `81a9fc0f3008e95bbdb10f17d0421bd364475ee532cb14341e9d3007803f5042`를 aapt2로 확인했다. source preset 또는 vendor changelog에서 추정한 목록이 아니다.

- `android.permission.INTERNET`
- `android.permission.CHANGE_WIFI_MULTICAST_STATE`
- `com.oculus.permission.HAND_TRACKING`
- `com.oculus.permission.RENDER_MODEL`
- `org.khronos.openxr.permission.OPENXR`
- `org.khronos.openxr.permission.OPENXR_SYSTEM`

이 APK에는 RECORD_AUDIO·CAMERA·외부 저장소 READ/WRITE/MANAGE·Scene 권한 선언이 없다. **PC 소리 수신에 마이크 승인 안내를 붙이지 않는다.** 위6개 선언이6개의 사용자 팝업을 뜻하지 않는다. 실제 새 설치·최초 실행의 시스템 안내와 손 추적 거절/컨트롤러 동작은 아직 검증하지 않았다. 공개 clean build에서 권한을 다시 읽고 손 추적 선언이 실제 제공 기능에 필요한지도 확인한다.

2026-10-01 별도 소스 재빌드의 unsigned APK `a3e933791f9ee047619232ac56f0b350e1a0524151e13e8cd7437ede01bf54de`에서도 실제 manifest의 같은6개 선언을 확인했다. public package `app.questto3d.client`, debuggable=false이며 마이크·카메라·저장소·Scene 선언을 추가하지 않았다. 전체 export·native 대응 확인과 설치/첫 실행 승인은 별도다. 이 후보는 아직 서명·설치하지 않았고 현재 개발 앱을 교체하지 않았다. [Quest 빌드 범위와 남은 조건](QUEST_RELEASE_PREPARATION_2026-09-30.md)

## 보안 차단과 설치 취소

현재 설치 PS1은 Authenticode 서명이 없다. GitHub 다운로드의 SmartScreen·Mark-of-Web·Smart App Control·회사 정책에 따른 첫 실행은 미검증이다. 다운로드 SHA 확인은 파일 무결성이고 게시자 인증과 다르다. 차단 화면에서는 파일·버전·공식 배포 출처를 확인하도록 설명하고 차단 종류를 구분한다. 백신·SmartScreen·방화벽을 통째로 끄는 안내를 기본 설치법으로 쓰지 않는다. 모든 차단이 사용자의 단순 실행 버튼으로 풀린다고 보장하지 않는다. [Microsoft 앱·브라우저 제어](https://support.microsoft.com/en-us/windows/security/windows-security/app-browser-control-in-the-windows-security-app)

CMD의 `-ExecutionPolicy Bypass`는 새 PowerShell 프로세스에만 적용하며 시스템의 영구 실행 정책을 바꾸지 않는다. 조직 GPO가 우선하므로 관리 PC의 정책을 우회하는 설치법으로 설명하지 않는다. [Microsoft 실행 정책](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies?view=powershell-5.1)

현재 PC/Quest 설치창은 시작 전 닫을 수 있지만 worker 실행 후 Cancel·timeout 기능은 없다. 작업 중 창 닫기를 제한하고 최소화·완료/실패 후 재시도를 안내한다. 안전한 취소를 지원한다고 표시하지 않는다. 처음 설치 실패는 파일·캐시를 보존하고 업데이트 실패/중단은 별도 복원 기능으로 다룬다.

## 보완 구현과 남은 검증

일반 설치·읽기 검사·정상 실행에는 새 확인 팝업을 추가하지 않는다. 연결 허용은 클릭 전 버튼 tooltip으로 범위를 설명하고 별도 OK/Cancel 창을 띄우지 않는다. 이미 정확한 앱 규칙이면 UAC도 생략한다. 유효한 조회 결과가 관리자 확인 필요 상태면 설치창에 이유를 표시한 뒤 필요한 Windows 승인만 요청한다. 앱 방화벽을 정리하며 제거할 때는 앱/데이터 보관 의미를 설명하는 기존 한 번의 확인을 유지한다. 제거할 앱 규칙이 명확히 없으면 새 조회로 확인한 뒤 일반 사용자 보관 제거를 진행한다. 조회 오류·소유권 충돌은 부재로 판정하지 않는다. 잘못된 결과 파일·변경된 실행 파일은 관리자 요청을 시작하지 않는다.

| 항목 | 소스의 보완 | 실제 OS/배포 검증 |
|---|---|---|
| 승인 이후 결과 | 자식 종료 코드·새 GUID 결과·설치 호스트·최종 규칙 대조. 취소·실패·부분 결과·중복 클릭 구분 | 실제 UAC 승인/취소 미검증 |
| 요청 전 파일 | 설치 owner/manifest와 helper·PowerShell·설정·host SHA 대조 | 새 표준 사용자/다른 관리자 계정 미검증 |
| 조회 오류 | 오류에서 변경/성공 판단 중단. known/unknown/absent 구분 | 실제 조직 정책·사용자별 조회 권한 미검증 |
| 결과 경로 | 설치 root/config/결과 ancestor·reparse 검사, CreateNew와 쓰기 사전검사 | 실제 junction·파일 충돌 검사 통과. 실제 ACL 거부는 별도 |
| 부분 삭제·생성 실패 | 남은 규칙·이번 생성 rollback·소유자 변경·기록 실패 보고 | 모의 cmdlet. 실제 Windows 규칙 변경 미검증 |
| 제거 | 방화벽만 승격, 원사용자 바로가기/개인 파일·모델·페어링 보관 | 실제 COM fixture와 보관 제거 검사. 다른 관리자 로그인 UI 미검증 |
| 초기 실행 오류 | CMD/추적 launcher에서 GUI 시작 전 오류·종료·로컬 로그 표시 | 실제 PS5 child fixture. 다운로드 Mark-of-Web/GPO/SmartScreen/SAC 미검증 |
| Python 충돌 | 현재 고정 Python/CLI 선택 경로 | GUI 선택 개선·새 Windows 공식 per-user 설치/C Runtime 예외 미완료 |
| 안전한 작업 취소 | 작업 중 중복 실행·창 닫기 차단, 실패/완료 후 재시도 | worker의 단계별 Cancel/timeout 미구현. 강제 종료를 안전한 취소로 표시하지 않음 |

네트워크 정책은 PS7과 Windows PowerShell5.1 모의 규칙·실제 receipt/파일/junction 및 parser를 포함한 51개 검사를 통과했다. root가 별도의 Python3.12.6/pytest8.4.2/psutil7.0.0 환경에서도 같은51개를 재실행해 통과했으며 실패·오류·skip은 없다. GPU/Qt 라이브러리가 없는 검사 환경이다. 설치 화면·추적 launcher와 기존 lifecycle의 별도 PS5 검사는 72개 통과, 106.65초다. UAC 호출은 fixture로 대체했으며 실제 CMD 시작 실패·로그, 원사용자 보관 제거·receipt 만료/재사용·재조회·중복 작업·취소/실패 분기를 검사했다. 최종 Apply 승인 최소화 변경은 관련 PS5 회귀17개·18.72초를 통과했다. 실제 설치창 tooltip 구성과 tracked launcher를 사용했고, Apply의 자체 확인0·필요한 UAC 요청1, 이미 적용0/0, 손상 조회0/0을 fixture로 확인했다. 이17개를 앞72개와 합산하지 않는다. 최종 공개 후보 자체 검사는 별도 기록한다. 이 수치는 실제 UAC·방화벽 변경·Quest 착용 또는 연결 성공을 증명하지 않는다.

구현: `scripts/release/install-ui.ps1`, `installer-network-task.ps1`, `installer-launcher.ps1`, `uninstall.ps1`, `native/host/configure-installed-network.ps1`. 검증: `tests/test_installed_network.py`, `tests/test_installer_permissions.py`, 기존 installation lifecycle/Quest installer/archive 검사. 새 결과는 `config/network-operations/<GUID32>/result.json`, 호환 journal은 `config/network-rules.json`이다. Status는 방화벽과 호환 journal을 변경하지 않는다. 결과 파일은 프로그램 경로가 들어가는 개인 로컬 자료이며 원문 공개를 기본값으로 하지 않는다.

방화벽 Allow가 존재해도 다른 명시적 Block 또는 조직 정책이 우선할 수 있다. 앱 소유 규칙 확인과 전체 유효 정책·실제 접속은 별도다. 상태 진단은 exact-program Block·네트워크 프로필을 가능한 범위에서 확인하고, 읽지 못한 정보는 unknown으로 남기며 외부 규칙을 자동 삭제하지 않는다. [Microsoft 규칙 우선순위](https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/rules)

Scan은 Sunshine Windows의 `DnsServiceRegister` 경로도 사용한다. 주소 연결 성공과 Scan 성공은 별도이며 host UDP5353을 무조건 추가하는 것으로 완료 처리하지 않는다. DNS Client·사설망 multicast·공유기 격리·여러 NIC/VPN이 별도 확인 조건이다. 이미 사용 중인 개발 규칙과 새 LocalSubnet 배포 규칙도 혼동하지 않는다.
