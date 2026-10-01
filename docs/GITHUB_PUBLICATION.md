# GitHub 공개·릴리스 운영

현재 사용자 설치는 [0.1.2 EXE 설치 안내](EXE_INSTALLERS.md)를 따른다. Desktop/Quest Setup EXE가 기본이며 아래 0.1.0 게시 증거·파일 목록은 당시 기록이다. 0.1.0 APK/Quest ZIP은 개인정보 수정 과정에서 철회됐으므로 새 설치에 사용하지 않는다. 새 검증 결과는 해당 Release의 보고서를 따른다.

기준: **2026-10-01 공개 절차**. 대상은 `myidwe/Quest3D`다. 이메일·토큰·서명 개인키는 공개 파일과 커밋에 넣지 않는다. 원격 생성·push·Release 공개 여부는 실제 GitHub 결과를 확인해 기록한다. 로컬 `origin` 설정은 업로드 증거가 아니다.

<details>
<summary>최초 0.1.0-preview 게시 기록 — 다운로드용 안내 아님</summary>

## 첫 Preview 게시 결과 — 2026-10-01

[0.1.0-preview 다운로드](https://github.com/myidwe/Quest3D/releases/tag/v0.1.0-preview)를 공개했다. Desktop ZIP, Quest ZIP, 동일한 standalone APK, native 대응 Source ZIP, 검증 JSON과 해시 파일을 로그인 없이 실제로 다운로드해 원본과 대조했다. 총 6개 파일의 크기·SHA가 일치한다.

Release 태그는 `50ff8d219637a3d99eab1070e0ce85b5e2d0e94c`에 고정했다. 해당 소스의 [GitHub CI](https://github.com/myidwe/Quest3D/actions/runs/36814730156)는 686검사 통과다. APK는 공개 패키지·release 인증서를 유지한 versionCode2다. 실제 PC 설정·페어링 보존 업데이트와 Quest 3의 code1→2 업데이트·기존 Pair·HEVC 수신을 확인했다.

새 UI의 착용 확인, Quest 2의 새 공개 APK, Python 없는 새 Windows, 다른 PC·GPU, 정량 AV·장시간은 미검증으로 남겼다. 실제 범위는 Release의 `release-validation.json`을 따른다. 게시된 설치 파일은 변경하지 않으며 이후 문서 갱신과 Release의 고정 소스 커밋을 구분한다.

</details>

## 공개 단위

**소스 저장소 공개와 설치 파일 Release는 별도 단계**다. 개인정보·저작권·필수 소스 파일의 공개 검사를 통과한 프로젝트 소스는 먼저 올릴 수 있다. README에는 다운로드 가능한 설치본의 유무와 native 대응 소스 준비 상태를 명시한다. 저장소 공개를 설치 가능·전체 재빌드 가능·최종 실기 검증 완료로 표시하지 않는다.

Desktop/Quest 설치 파일은 실제 바이너리의 대응 소스·제3자 고지·APK 서명·설치 안전성을 마감한 별도 Release로 제공한다. 현재 로컬 review 후보를 이름만 바꿔 공개판으로 게시하지 않는다. 최신 Quest 2 UI, 다른 PC, 사용자 청취·AV 오차·장시간처럼 아직 확인하지 못한 조건은 Preview 지원표에 남길 수 있다. 모든 장비와 모든 성능 검증을 소스 공개의 필수 조건으로 확대하지 않는다.

[이번 공개 체크리스트](PUBLIC_RELEASE_CHECKLIST_2026-10-01.md)의 필수 조건과 Preview 미검증 항목을 구분한다.

## 사용자가 만나는 화면

README → **Desktop Setup EXE / Quest Setup EXE** → [처음 설치와 연결](GETTING_STARTED.md) → PC 시작 → Quest Scan / Pair / Connect

첫 Release 안내에는 설치 순서·현재 지원 GPU·주요 제한을 먼저 배치한다. 구현 내부의 긴 개발 기록은 유지보수 문서로 연결한다. 자동 생성되는 Source code ZIP은 일반 사용자 설치 파일로 안내하지 않는다. 공개 파일은 한 버전으로 묶고 기존 파일을 조용히 바꿔치기하지 않는다.

| 배포 파일 | 처음 사용할 때 |
|---|---|
| Quest3D-Desktop-Setup-0.1.2-preview.exe | 실행 → 설치 → 연결 허용 → 설치창 닫기 → PC 앱 실행 |
| Quest3D-Quest-Setup-0.1.2-preview.exe | Windows에서 실행 → adb.exe 선택 → 기기 검색 → Quest 선택 → 설치 |
| Quest3D-Source-0.1.2-preview.zip | 개발자용 native 대응 소스·입력·라이선스·재현 안내 |
| SHA256SUMS.txt | 최종 파일의 SHA-256 |
| release-validation.json | 해당 배포판에서 확인한 결과와 남은 조건 |

이 표는 현재 공개 Preview의 파일 이름이다. 다운로드는 [0.1.2-preview Release](https://github.com/myidwe/Quest3D/releases/tag/v0.1.2-preview)를 사용한다. 현재 개발 서명 APK와 로컬 review ZIP을 정식 공개판으로 표시하지 않는다. PC 설치 후 Python·GPU 라이브러리·기본 모델 다운로드는 설치창이 담당한다. Quest 개발자 모드와 헤드셋의 USB 승인, Google Platform Tools 준비는 최초 한 번 필요한 사용자 작업이다.

## 안전한 소스 준비

원본 작업 폴더는 개인 실행 상태·실험·키·모델이 있는 개발 폴더다. 원본에서 `git add .`를 실행하지 않는다. 새 위치로 allowlist export → 파일 내용/해시 검사 → 별도 Git 저장소 생성 → 실제 Git tree 감사 순서로 진행한다.

```powershell
python -B scripts/release/export_repository.py --output artifacts/publication/new-export
python -B scripts/release/export_repository.py --output artifacts/publication/new-export --verify
python -B scripts/release/prepare_github.py --export artifacts/publication/new-export --output artifacts/publication/new-git --owner myidwe --name Quest3D --github-user-id 336009611
```

경로는 매번 새 폴더여야 한다. 마지막 도구는 **로컬 main 커밋과 origin 주소만 준비**하며 GitHub 로그인·저장소 생성·push·Release 게시를 하지 않는다. 커밋 이메일은 확인한 공개 GitHub ID의 noreply 주소를 사용한다. export manifest는 변경되지 않은 별도 후보에 보관한다. Git의 줄바꿈 정규화 결과는 `git-tree-review.json`에 따로 기록하며 설치 manifest와 혼용하지 않는다.

Git tree에는 가중치·venv·capture/host 실행 파일·APK·서명키·페어링·실기 로그가 들어가지 않는다. 큰 실행 파일과 대응 소스 archive는 Release assets로 제공한다. 사용자 설치 폴더나 이전 검토 폴더를 Git에 추가하지 않는다.

## 저장소 생성

[GitHub 새 저장소 화면](https://github.com/new?owner=myidwe&name=Quest3D&visibility=public&description=Local%20AI%20stereo%203D%20desktop%20streaming%20for%20Meta%20Quest)을 사용하면 owner/name/설명을 준비할 수 있다. 이 링크는 **생성 양식**이고 이미 생성된 저장소 주소가 아니다. 감사한 소스만 게시하는 첫 단계는 Public 저장소로 진행할 수 있다. 설치 파일 준비는 Draft Release에서 계속한다.

Owner `myidwe`, Name `Quest3D`, Default branch `main`, GPL-3.0. 기존 준비 소스를 올리므로 웹에서 README·.gitignore·LICENSE를 자동 생성하지 않는다. 설명은 `Local AI stereo 3D desktop streaming for Meta Quest`. Topics 후보는 `meta-quest`, `stereo-3d`, `depth-estimation`, `windows`, `local-ai`.

사용 가능한 GitHub connector·웹·CLI 중 실제 계정·권한을 확인할 수 있는 경로로 생성·push·asset 업로드한다. connector 로그인과 PC Git/gh 인증은 별개다. 별도 인증이 필요하면 공식 로그인 흐름을 사용하며 비밀번호나 토큰을 대화에 붙여 넣지 않는다.

GitHub CLI를 사용한다면 별도 로그인 후 `gh api user --jq .login`이 `myidwe`인지 확인한다. 정확한 staging 경로와 감사한 커밋을 확인한 뒤 저장소를 만들고 main만 push한다. 강제 push·원본 폴더 추가·이미 있는 저장소 덮어쓰기는 기본 절차에 없다. [공식 저장소 생성 안내](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)

웹에서 빈 저장소를 만들었다면 **감사한 별도 Git 후보의 `repository` 폴더**에서 아래 명령으로 main을 올린다. 로컬 도구가 origin을 이미 준비하므로 다시 추가하지 않는다. 로그인된 GitHub 계정과 origin이 대상과 일치하는지 먼저 확인한다. 명령 예시는 실행 결과를 의미하지 않는다.

```powershell
git status --short
git remote get-url origin
git log -1 --oneline
git push -u origin main
```

origin 예정 값은 `https://github.com/myidwe/Quest3D.git`이다. 기존 저장소가 발견되거나 이력 충돌이 나면 강제 push로 덮어쓰지 않는다. 최종 다운로드 링크는 검증된 Release가 실제로 만들어진 후 README에 넣는다.

## 게시 판단

| 단위 | 게시에 필요한 확인 | Preview에 남길 수 있는 조건 |
|---|---|---|
| 소스 저장소 | 실제 Git tree의 allowlist·개인값/키 제외, GPL/제3자 고지, build 자료와 미완료 범위, 계정·원격·커밋 확인 | native 전체 재빌드·설치 파일 준비, 미검증 장비·착용·성능 |
| 설치 파일 Release | 해당 binary/source 연결, 필요한 source/recipe/notice 공급, 설치할 수 있는 signed APK, package/version 정책, 설치·업데이트·실패 보존 검사, 최종 ZIP/해시/다운로드 대조 | 새 Windows의 모든 조합, 최신 Quest 2 착용 재검증, 장시간·AV 수치, 지원표 밖의 GPU |

새 바이너리를 선택하면 PC 시작·종료·실제 캡처/AI/송출과 해당 Quest APK의 설치·연결·양안 표시를 확인한다. 앞선 운영 바이너리의 동작을 새 후보의 결과로 대신하지 않는다. 사용자가 아직 착용 확인하지 못했으면 그 상태를 공개하고, 확인한 기종·경로로 지원 범위를 제한한다.

실제 데이터 삭제·과도한 네트워크 개방·서명 불일치·미완성 대응 소스 같은 오류는 Preview 표시로 덮지 않는다. 반면 누락된 장시간 수치만으로 이미 감사한 소스 게시를 미루지 않는다. CI는 소스 경계/설치 분기/자료 무결성 검사이며 실제 GPU·Quest 성능 검증을 대신하지 않는다.

## Release와 이후 업데이트

다음 버전의 새 태그를 실제 공개 커밋에 연결하고 **Draft + Pre-release**로 준비한다. PC·Quest·대응 소스·해시·검증 요약을 모두 첨부한 뒤 파일을 다시 내려받아 검증한다. 그 다음 공개하고 README의 다운로드 링크를 실제 URL로 바꾼다. Draft/Prerelease 기능은 [GitHub 공식 Release 안내](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)를 따른다. 파일 하나는 2 GiB 미만이어야 하며 큰 바이너리·전체 대응 소스는 Git tree에 추가하지 않는다. [GitHub Release 용량 기준](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)

출시 담당자는 게시 직후 README 링크·Release asset 목록·태그 커밋·SHA256SUMS를 로그인하지 않은 화면에서도 확인한다. 설치 파일이 아직 없을 때는 Releases를 작동하는 Download 버튼으로 안내하지 않는다. 다음 버전은 새 태그·새 파일로 만들며 이미 공개한 파일의 내용을 같은 이름으로 교체하지 않는다.

일반 사용자의 다음 업데이트는 새 Desktop Setup EXE → 동일 설치 폴더 → 업데이트 → 검증 → 설치창 닫기 → 실행. 모델·설정·페어링은 유지한다. host 경로/해시나 Python 버전이 바뀌는 비호환 업데이트는 현재 updater가 차단하므로 별도 마이그레이션 버전으로 다룬다. Quest는 동일 package와 서명을 유지하고 versionCode를 올려 기존 앱 데이터 보존 설치한다. 키 변경은 단순 업데이트가 아니므로 별도 안내가 필요하다. [Android 공식 서명 안내](https://developer.android.com/studio/publish/app-signing)

버전마다 CHANGELOG·설치 안내·지원표·실측 조건을 함께 갱신한다. Issues에서 사용자 환경·재현 단계·예상/실제 동작을 받고, 개인 로그·인증서·IP·기기 ID는 공유 전 검토하도록 안내한다. 무료 앱의 첫 공개 범위는 검증된 Windows/NVIDIA Turing이며 다른 GPU 지원은 별도 실측 후 확대한다.

[설치](DISTRIBUTION.md) · [빌드](BUILDING.md) · [공개 체크리스트](PUBLIC_RELEASE_CHECKLIST_2026-10-01.md) · [공개 준비 계획](OPEN_SOURCE_RELEASE_PLAN_2026-09-30.md)
