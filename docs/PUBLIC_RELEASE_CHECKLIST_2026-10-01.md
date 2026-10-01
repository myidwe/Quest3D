# 첫 GitHub 공개 체크리스트

2026-10-01 · 소스 공개와 설치 파일 Release를 분리한 실행 기준

이 문서는 작업 착수 시점의 실제 증거와 남은 일을 정리한다. 로컬 후보의 `published: false`는 해당 도구가 게시를 하지 않았다는 뜻이며 이후 원격 상태를 자동 추적하지 않는다. 최종 게시 기록은 GitHub의 저장소·커밋·Release URL과 실제 다운로드 검사 결과로 남긴다.

## 1. 소스 저장소

| 항목 | 확인한 상태 | 완료 기준 |
|---|---|---|
| 원본 보존 | 원본 개발 폴더와 e 후보를 분리 | 원본에서 `git add .` 금지, 새 export/Git 경로 사용 |
| 공개 파일 | e는 allowlist 646파일, 개인 상태·모델·실행 파일·APK·키 제외 | 최종 선택 목록과 실제 Git blob 해시 감사 |
| 기본 검사 | e 후보 자체 430검사 통과 | 이후 변경 관련 검사와 최종 export 검증, 수치를 이전 결과와 구분 |
| 공개 고지 | GPLv3·모델·폰트·아이콘·다운로드 의존성 고지 포함 | 공개한 실제 코드의 원 저작권·약관 보존 |
| 사용 안내 | 한국어·영어 README, 설치·빌드·지원표 | 소스만으로 바로 설치/전체 재빌드할 수 있는 것처럼 표시하지 않음 |
| GitHub 인증 | 연결 계정은 앞선 확인에서 `myidwe` | 이번 write 경로의 실제 계정·owner·권한 확인 |
| 원격 게시 | 착수 시점에는 로컬 main/origin만 준비 | 원격 저장소 존재·visibility·main commit을 read-back |

저작권 고지의 upstream 저자 이메일과 테스트용 사설 IP는 개인 인증 정보가 아니다. 실제 소유자의 개인 이메일·home·인증서·로그와 구분한다. 검사 결과에 민감한 값 자체를 출력하지 않는다. 바이너리 archive 내부 감사는 Git의 텍스트 검사와 별도로 수행한다.

GitHub에는 감사한 소스를 먼저 공개할 수 있다. native release 준비·헤드셋 착용·다른 PC 검증을 모두 완료해야만 소스 공개가 가능한 것은 아니다. README의 설치 파일 상태를 함께 갱신한다.

## 2. 설치 파일의 필수 조건

| 항목 | 착수 시점 상태 | 남은 작업 |
|---|---|---|
| Host baseline | 기존 운영 binary와 새 native build 후보가 별개 | 선택한 exe에 실제 전체 수정 소스·입력·build recipe 연결 |
| Host 의존성 | 정확한 FFmpeg/MSYS 입력 식별, GNU 3개 source 수집 | 실제 static 구성의 source/recipe 공급과 필요한 notice를 새 asset에 반영 |
| Quest baseline | 새 public package의 full Android export·native 대응 검증 성공 | vendor 헤더·정적/Java 의존성의 실제 약관·notice·source 공급 마감 |
| APK 서명 | 새 APK는 unsigned, 기존 앱은 개발 package/서명 | 외부 보관 release key로 서명, 인증서·package·versionCode·payload 확인 |
| PC installer | e 격리 업데이트·설정/모델 보존·CUDA/QML·read-only Status 확인 | 새 binary pin을 반영한 후보로 설치·실패 보존·시작/종료 검증 |
| 기존 앱 업데이트 | host/Python 변경 시 현재 updater가 비호환 업데이트를 차단 | 첫 공개판은 별도 설치 안내 또는 검증한 migration; 강제 덮어쓰기 금지 |
| Quest 업데이트 | debug/public package가 다름 | 최초 공개판은 별도 앱으로 설치, 이후 같은 release 서명·높은 versionCode로 보존 설치 |
| 실제 제품 연결 | 기존 개발 PC/Quest 2·3의 영상 사용 기록 있음 | 새 host/APK의 실제 capture→AI→좌우→encoder→Quest 경로 확인 |
| 파일 공급 | e ZIP는 기존 binary의 review 묶음 | 새 Desktop/Quest/Source·해시·검증 요약을 같은 버전으로 생성·대조 |

새 host가 빌드됐다는 사실만으로 기존 운영 exe의 누락된 소스를 복구했다고 판단하지 않는다. 새 Quest가 빌드됐다는 사실만으로 기존 debug APK의 전체 대응 소스가 같다고 판단하지 않는다. 선택한 최종 바이너리에 맞춰 새 provenance를 만든다.

필요한 Corresponding Source는 실제 결합된 코드와 생성·수정에 필요한 자료를 기준으로 정한다. 모든 compiler·CMake·Ninja·Git·일반 도구를 다시 빌드하거나 기존 exe와 SHA가 완전히 같아야 한다는 조건을 추가하지 않는다. System Library와 적용되는 runtime exception의 제외 근거는 기록한다. [프로젝트 GPLv3 원문](../LICENSE) · [GNU GPLv3 정의](https://www.gnu.org/licenses/gpl-3.0.html)

Android APK는 설치 전에 서명돼야 한다. 장기 업데이트에 사용하는 키는 저장소·ZIP 밖에서 보관하며 개인키·암호를 대화/argv/log에 넣지 않는다. 공개 인증서 fingerprint는 배포 metadata로 제공할 수 있다. 서명키 보관·백업은 프로젝트 소유자에게 인계한다. [Android 공식 서명 안내](https://developer.android.com/studio/publish/app-signing)

Windows Authenticode 인증서 구매는 무료 Preview의 필수 조건으로 추가하지 않는다. 실제 OS 정책이 실행을 막는 경우 원인과 게시자/해시 확인 방법을 안내하며 보안 기능의 일괄 해제를 설치 절차로 만들지 않는다.

## 3. Preview에 표시할 검증 범위

현재 지원은 **Windows x64 / NVIDIA Turing sm75**, 실측 GPU는 **RTX 2060 SUPER 8 GB**, 모니터는 **16:9**다. 더 새 GPU·다른 OS를 검증 없이 지원 대상으로 늘리지 않는다.

| 검증 | Preview 공개 시 표시 |
|---|---|
| 실제 설치·CUDA/QML·설정 보존 | 최종 후보의 날짜·버전·같은 PC의 격리 설치임을 기재 |
| 새 Windows의 Python 자동 설치·UAC·다른 관리자 | 미검증이면 미검증, 실제 오류를 알고 있으면 수정 후 공개 |
| Quest 2/3 | 최종 후보로 확인한 기종만 해당 빌드 검증으로 기재, 이전 개발 앱 결과는 별도 |
| 새 3D FPS·수신 FPS·헤드셋 표시율 | 단계와 측정 조건을 분리, 전체 장면 최소 FPS 약속 금지 |
| 소리 | PC 기본, Quest only의 기존 가상 장치 조건, 실제 확인한 수신/복원 범위 |
| 착용·AV 오차·장시간 | 측정 전 숫자나 검증 완료 표시 금지 |
| 윤곽·가려진 배경 | 잔여 한계와 Depth/2D 조절 안내 |
| Scan | 사설망·mDNS·게스트 격리 조건, 주소 입력 대체 제공 |

하드웨어 미검증은 지원표의 제한으로 남길 수 있다. 서명 불일치·데이터 유실·과도한 방화벽 개방·실제 코드의 약관 미확인·불충분한 대응 소스는 설치 파일 공개 전에 해결한다. 현재 없다고 확인한 mic/camera/storage 권한을 가상의 설치 승인 단계로 추가하지 않는다.

## 4. 사용자가 받는 최소 파일

**Desktop ZIP + Quest ZIP**이 일반 사용자 경로다. Source ZIP과 checksum은 같은 Release에서 함께 제공한다. GitHub 자동 Source code ZIP은 설치 파일로 안내하지 않는다. [GitHub 공식 Release 안내](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

1. Desktop 전체 압축 해제 → `Install-Quest3D.cmd` → 설치
2. 연결 허용 → 필요한 경우 Windows UAC 한 번
3. Quest 개발자 모드·USB 디버깅 승인 → 공식 Platform Tools → `Install-Quest.cmd`
4. 같은 사설망 → PC 시작 → Quest Scan / Pair → PC PIN 승인
5. 다음 실행부터 PC 시작 → Quest Connect

Quest 개발자 모드·헤드셋 USB 승인과 Windows UAC는 사용자가 직접 완료한다. 설치창 자체의 의미 없는 중복 확인을 더하지 않는다. Platform Tools와 가상 오디오 드라이버를 준비하지 않은 사용자가 무엇을 할 수 있는지 명시한다. 기본 PC 소리는 추가 드라이버 없이 사용하며 Quest only는 조건부 기능이다.

일반 설치에는 source build 도구·Codex·WSL이 필요하지 않다. 처음 라이브러리·모델 다운로드가 수 GB이고 설치 cache/backup도 공간을 쓴다. 아직 실측하지 않은 최소 디스크·RAM·driver 수치를 임의 확정하지 않는다.

## 5. GitHub 완료 증거

- 감사한 final commit과 원격 main commit 일치
- 읽기 가능한 Public 저장소·정확한 LICENSE·README 링크
- 실제 GitHub Actions 결과를 로컬 pytest 결과와 구분
- final tag의 commit·Prerelease 표시·Desktop/Quest/Source/검증 요약 목록
- 업로드한 파일을 다시 다운로드해 크기·SHA256SUMS·내부 manifest 대조
- 로그인하지 않은 사용자의 다운로드 링크 확인
- README의 Download 링크가 실제 파일/Release를 가리킴
- Release note에 지원표·검증 범위·미검증 조건·설치/업데이트 안내

소스만 올린 시점에는 **소스 공개 완료, 설치 파일 Release 준비 중**으로 보고한다. 설치 파일도 게시하고 download 검사까지 끝났을 때 **Preview 배포 완료**로 보고한다. 사용자에게 남은 헤드셋 착용 확인을 전체 구현 실패와 혼동하지 않되 확인하지 않은 결과를 확인했다고 보고하지 않는다.

[GitHub 절차](GITHUB_PUBLICATION.md) · [설치 안내](DISTRIBUTION.md) · [빌드 안내](BUILDING.md)
