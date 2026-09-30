# GitHub 공개·릴리스 운영

상태: **2026-09-30 공개 준비안**. 대상 계정은 `myidwe`, 제안 저장소 이름은 `Quest3D`. 연결된 GitHub 사용자가 `myidwe`인 것은 직접 확인했다. 이메일·토큰·서명키는 공개 파일과 커밋에 넣지 않는다. 아직 원격 저장소를 생성하거나 소스를 업로드하지 않았다.

## 사용자가 만나는 화면

README → Releases → 같은 버전의 **Desktop ZIP / Quest ZIP** → 설치창 → PC 시작 → Quest Scan / Pair / Connect

첫 Release 안내에는 설치 순서·현재 지원 GPU·주요 제한을 먼저 배치한다. 구현 내부의 긴 개발 기록은 유지보수 문서로 연결한다. 자동 생성되는 Source code ZIP은 일반 사용자 설치 파일로 안내하지 않는다. 공개 파일은 한 버전으로 묶고 기존 파일을 조용히 바꿔치기하지 않는다.

| 배포 파일 | 처음 사용할 때 |
|---|---|
| Quest3D-Desktop-0.1.0-preview.zip | 전체 압축 해제 → Install-Quest3D.cmd → 설치 → 연결 허용 → 앱 실행 |
| Quest3D-Quest-0.1.0-preview.zip | 전체 압축 해제 → Install-Quest.cmd → adb.exe 선택 → 기기 검색 → Quest 선택 → 설치 |
| Quest3D-Source-0.1.0-preview.zip | 개발자용 native 대응 소스·입력·라이선스·재현 안내 |
| SHA256SUMS.txt | 최종 파일의 SHA-256 |
| release-validation.json | 해당 배포판에서 확인한 결과와 남은 조건 |

이 표는 예정 파일 이름이며 다운로드 링크가 아니다. 현재 개발 서명 APK와 로컬 review ZIP을 정식 공개판으로 표시하지 않는다. PC 설치 후 Python·GPU 라이브러리·기본 모델 다운로드는 설치창이 담당한다. Quest 개발자 모드와 헤드셋의 USB 승인, Google Platform Tools 준비는 최초 한 번 필요한 사용자 작업이다.

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

[GitHub 새 저장소 화면](https://github.com/new?owner=myidwe&name=Quest3D&visibility=private&description=Local%20AI%20stereo%203D%20desktop%20streaming%20for%20Meta%20Quest)을 사용하면 owner/name/설명을 준비할 수 있다. 이 링크는 **생성 양식**이고 이미 생성된 저장소 주소가 아니다. 처음은 Private로 준비한 뒤 아래 공개 기준을 충족하면 Public으로 전환하는 흐름을 권장한다.

Owner `myidwe`, Name `Quest3D`, Default branch `main`, GPL-3.0. 기존 준비 소스를 올리므로 웹에서 README·.gitignore·LICENSE를 자동 생성하지 않는다. 설명은 `Local AI stereo 3D desktop streaming for Meta Quest`. Topics 후보는 `meta-quest`, `stereo-3d`, `depth-estimation`, `windows`, `local-ai`.

현재 GitHub connector에서는 해당 계정 확인·저장소 조회가 가능하지만 저장소 생성/Release asset 업로드 도구는 제공되지 않는다. GitHub 웹 또는 별도 GitHub CLI 로그인으로 생성·push·asset 업로드한다. connector 로그인과 PC Git/gh 인증은 별개다. 비밀번호나 토큰을 대화에 붙여 넣지 않는다.

GitHub CLI를 사용한다면 별도 로그인 후 `gh api user --jq .login`이 `myidwe`인지 확인한다. 정확한 staging 경로와 감사한 커밋을 확인한 뒤 저장소를 만들고 main만 push한다. 강제 push·원본 폴더 추가·이미 있는 저장소 덮어쓰기는 기본 절차에 없다. [공식 저장소 생성 안내](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)

웹에서 빈 Private 저장소를 만들었다면 **감사한 별도 Git 후보의 `repository` 폴더**에서 아래 명령으로 main을 올린다. 로컬 도구가 origin을 이미 준비하므로 다시 추가하지 않는다. 로그인된 GitHub 계정과 origin이 대상과 일치하는지 먼저 확인한다. 이 명령은 아직 실행하지 않았다.

```powershell
git status --short
git remote get-url origin
git log -1 --oneline
git push -u origin main
```

origin 예정 값은 `https://github.com/myidwe/Quest3D.git`이다. 기존 저장소가 발견되거나 이력 충돌이 나면 강제 push로 덮어쓰지 않는다. 최종 다운로드 링크는 검증된 Release가 실제로 만들어진 후 README에 넣는다.

## 첫 공개 기준

- 실제 배포 바이너리에 연결된 host/Quest 전체 대응 소스와 빌드 입력·고지
- 최신 APK의 source/native 일치, 공개 package·장기 서명·versionCode 증가 정책
- 새 Windows 설치·실제 CUDA/QML·바로가기·방화벽·Scan/Pair/영상
- 업데이트 실패·중단 시 원복, 설정·모델·페어링 보존
- Quest 2/3 최종 설치본의 양안·설정·재연결 확인
- 실제 확인한 성능·오디오·장시간 결과와 미검증 조건의 구분
- 최종 Git tree·ZIP·내부 tar·키/개인값 감사 및 다운로드 해시

검토한 범위의 미검증 항목은 Preview 제한으로 명시할 수 있지만, 대응 소스·라이선스·서명·설치 안전성 오류를 Preview라는 이유로 생략하지 않는다. CI는 소스 경계/설치 분기/자료 무결성 검사이며 실제 GPU·Quest 성능 검증을 대신하지 않는다.

## Release와 이후 업데이트

`v0.1.0-preview` 태그를 실제 공개 커밋에 연결하고 **Draft + Pre-release**로 준비한다. PC·Quest·대응 소스·해시·검증 요약을 모두 첨부한 뒤 파일을 다시 내려받아 검증한다. 그 다음 공개하고 README의 다운로드 링크를 실제 URL로 바꾼다. Draft/Prerelease 기능은 [GitHub 공식 Release 안내](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)를 따른다.

일반 사용자의 다음 업데이트는 새 ZIP → 동일 설치 폴더 → 업데이트 → 검증 → 실행. 모델·설정·페어링은 유지한다. host 경로/해시나 Python 버전이 바뀌는 비호환 업데이트는 현재 updater가 차단하므로 별도 마이그레이션 버전으로 다룬다. Quest는 동일 package와 서명을 유지하고 versionCode를 올려 기존 앱 데이터 보존 설치한다. 키 변경은 단순 업데이트가 아니므로 별도 안내가 필요하다. [Android 공식 서명 안내](https://developer.android.com/studio/publish/app-signing)

버전마다 CHANGELOG·설치 안내·지원표·실측 조건을 함께 갱신한다. Issues에서 사용자 환경·재현 단계·예상/실제 동작을 받고, 개인 로그·인증서·IP·기기 ID는 공유 전 검토하도록 안내한다. 무료 앱의 첫 공개 범위는 검증된 Windows/NVIDIA Turing이며 다른 GPU 지원은 별도 실측 후 확대한다.

[설치](DISTRIBUTION.md) · [빌드](BUILDING.md) · [공개 준비 계획](OPEN_SOURCE_RELEASE_PLAN_2026-09-30.md)
