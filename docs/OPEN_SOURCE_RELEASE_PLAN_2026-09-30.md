# GitHub 공개 준비

기준일: 2026-10-01. [myidwe/Quest3D](https://github.com/myidwe/Quest3D) Public 저장소와 `main` 소스 게시를 완료했다. 첫 원격 커밋 `3f96913ca6c59cbc21811d40d9337a628224730c`의 [GitHub CI](https://github.com/myidwe/Quest3D/actions/runs/36808111632)는 **430검사 통과·139.26초**, 646파일 export 검증도 통과했다. 설치 파일 Release는 새 host/공개 APK의 대응 소스·고지·서명·설치 검증을 마감하며 준비 중이다. 소스 게시와 설치 배포 완료를 구분한다. 원본 작업 폴더는 Git 저장소가 아니며 공개 후보와 분리한다.

아래 이전 후보 준비 기록의 과거 '원격 게시 전' 상태는 첫 게시 이전에 확인한 증거다. 현재 실행 기준은 [공개 체크리스트](PUBLIC_RELEASE_CHECKLIST_2026-10-01.md)를 따른다.

## 이전 후보의 준비 기록

새 A 드라이브 폴더에서 실제 설치607.391초, 모델 새 다운로드·CUDA7종·실제 모델 CUDA추론·Qt6.8.3 QML을 확인했다. 같은 폴더의 c 후보 업데이트343.641초에서도 설정·모델·receipt·비기능 pairing fixture·사용자파일 SHA가 유지됐다. 기존 Python3.12.6을 지정한 이 PC의 분리 설치 결과이며 Python 없는 새 Windows의 자동 설치 결과가 아니다. 방화벽·바로가기·host 송출·Quest 재설치는 실행하지 않았다.

실제1029파일 설치에서 경로 검사 비용을 확인하고 모든 ancestor 검사를 native IO로 유지해 GetOwned15.23→0.68초, 실제 PS5.1 UI SelfTest169.52→13.36초로 줄였다. 관련 최종101검사와 독립 검토를 통과했다. 이 개선은 다음 후보에 반영하며 c 설치 완료 증거와 분리한다. UI가 항상 즉시 반응한다는 검증은 아니다.

대상 GitHub 연결 계정 `myidwe`를 직접 확인했다. `myidwe/Quest3D`를 제안 대상으로 한 [게시·릴리스 절차](GITHUB_PUBLICATION.md), 영문 README, immutable export와 분리한 실제 로컬 Git 준비 도구, ZIP 세트 내부 해시/소스 연결 감사 도구를 추가했다. 원격 생성·push는 아직 수행하지 않았다.

PC installer에 앱 소유권·compatible 버전 업데이트·실패/중단 원복·바로가기 교체·보관 제거를 구현했다. 실제 PS5.1 fixture에서 설정·모델·페어링 보존과 실패 주입을 확인하며, 완전한 최신 설치본의 fresh GPU/Quest 테스트와 구분한다. Quest 설치창에는 다중 기기 선택·승인 대기/오프라인 구분·버전 감소 차단을 추가했다. [설치 안내](DISTRIBUTION.md)

최신 Quest source manifest와 보존 native 입력을 수집했고 실제 mDNS 소스 누락을 수정했다. 역사적 Sunshine 소스는 isolated configure 후 실제 build에서 누락 변경 때문에 실패했다. 따라서 전체 대응 소스·clean build·고지가 완료되었다고 승격하지 않는다. 기존 제품 runtime/사용자 설정/설치 APK는 보존한다. [호스트 소스 결과](HOST_RELEASE_PREPARATION_2026-09-30.md)

## 권장 공개 방식

첫 공개는 **GPLv3 소스 + 설치 파일을 제공하는 제한된 0.1.0-preview**. 일반 사용자는 Releases의 PC ZIP과 Quest ZIP을 사용하고, 개발자는 저장소와 별도 native 대응 소스를 받는다. 복잡한 native 빌드와 WSL을 일반 설치 과정에 넣지 않는다.

GitHub Releases는 태그에 묶인 실행 파일과 안내를 제공하고 자동 Source code ZIP도 생성한다. 자동 ZIP은 저장소 내용만 담으므로 별도 native 입력·대응 소스가 빠지지 않도록 해야 한다. 실행 파일·수백 MB upstream 소스는 Release assets, 모델은 고정 revision 다운로드로 분리한다. [GitHub 공식 Releases 안내](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

## 다운로드 경험

README 첫 부분에 **Windows 다운로드 / Quest 설치 / 시작 안내**를 배치한다. 공개 시 실제 저장소 주소로 Releases·파일 링크를 연결한다. 미완성 파일 링크나 가상의 다운로드 주소를 사용하지 않는다.

| 배포물 | 내용 |
|---|---|
| Quest3D-Desktop-<version>.zip | PC 설치창, Python/QML 소스·자원, 고정 host/capture/probe, 라이선스 |
| Quest3D-Quest-<version>.zip | APK, USB 설치창, 기기/해시 검사, 고지·설치 안내 |
| Quest3D-Source-<version>.zip | exact upstream·수정분·필수 submodule·lock·빌드 지침 |
| SHA256SUMS.txt | 최종 파일 해시 |
| release-validation.json | 공개 가능한 기능·설치·재빌드·실기 검증 요약 |

Quest ZIP 안 APK를 별도로 올릴 수 있지만 같은 버전·서명·해시의 단일 파일이어야 한다. GitHub 자동 Source code ZIP을 PC 설치 파일로 안내하지 않는다. 모델·Torch/CUDA 의존성까지 몇 GB를 한 번에 저장소에 올리는 방식은 피한다.

## 이전 검토 시점의 구현 현황

실제 Windows 모니터 → GPU 캡처 → 로컬 AI → 좌우 합성 → Sunshine/NVENC → Quest 눈별 표시가 작동한다. Quest 2·Quest 3, PC UI, Scan/Pair, Depth 미세 조절, 윤곽 안정화, HEVC, DAv2/DAD 선택, Quality Preview, 화면·메뉴 설정, PC/Quest 소리 선택이 구현되어 있다.

사용 중인 PC 코드는9/30 Sound, Quest APK는9/30 Level 설정까지다. 9/30의 d 검토 ZIP/Git 후보는 보존하며 10/1 설치·권한 보완은 새 후보로 준비한다. GitHub 공개 ZIP은 아직 없다. Quest 3의 최초 영상·입체감은 사용자에게 확인받았고, README의 오래된 연결 대기 문구를 수정했다. 최근 UI의 착용 평가와 오디오 청취는 아직 별도 확인 대상이다.

| 항목 | 현재 상태 | 공개 기준 |
|---|---|---|
| 실제 AI·양안·Quest 감상 | 개발 장비 확인 | 최종 배포 설치본 재확인 |
| PC 문서·지원 범위 | 이번 검토에서 갱신 | 공개 파일/링크와 일치 |
| 개인 자료 분리 | allowlist·해시·내용 검사 도구 준비 | 최종 Git tree/ZIP/내부 tar 감사 |
| 고정 host 대응 소스 | 역사 source 실제 빌드 실패. 독립 API 후보 native/web 빌드·CLI 통과, 송출 미검증 | 새 완전 소스·고지·바이너리 고정 및 기능/마이그레이션 확인 |
| 최신 Quest 전체 소스 | source r3 489파일·새 engine/stream/XR 컴파일·전체 project/Java/DEX export 성공. unsigned public APK와 strip 후 native 대응 확인 | 활성 vendor/DEX 소스·고지 공급 마감, 서명·설치·실기 |
| Quest 의존성 고지 | 추가 감사 필요 | 6native와 정적 의존성의 실제 목록·고지 |
| 공개 APK 서명 | 개발 package/debug 키 | 장기 release 키·package·version 정책 확정 |
| PC 업데이트 | 소유권 기반 compatible 업데이트·원복·보관 제거 구현, PS5.1 fixture 통과 | 최종 실제 설치본 업데이트와 비호환 host migration |
| 새 PC 설치 | 같은 PC의 이전 새폴더 검사 | Python 실제 설치·바로가기·방화벽·새 PIN |
| Scan | 개발망 Quest 3 확인 | fresh PC mDNS5353·게스트/AP 격리 실패 안내 |
| Audio | Quest 3 전송·출력복원 확인 | 실제 청취·AV 오차·Quest 2·장시간 |
| 성능 | PC 처리 측정 있음 | 최종 배포판 종단 측정·drop·누적 지연 |

## 확인한 PC와 지원 조건

2026-09-30 직접 조회: Windows 11 Education `10.0.26200`, Intel Core i5-12600KF, RAM 약63.85GiB, RTX2060 SUPER8192MiB, 드라이버616.56. 이것은 실측 환경이며 최소 사양 표가 아니다. 최소 CPU/RAM/설치 용량과 다른 드라이버 범위는 아직 정하지 않는다.

현재 커널은 NVIDIA Turing sm75 전용이다. RTX30/40/50·AMD·Intel이 자동 지원되는 상태가 아니다. 기본 출력은 Quest2 eye1920×1080, Quest3 eye2048×1152·HEVC,16:9. AI 입력은 Standard504×280, Quality574×322로 영상·눈별·패널 해상도와 다르다. 같은 사설망을 사용하고 USB 영상 전송은 제공하지 않는다.

9/22 PC 측정 DAD Standard38.66 new3D/s, Quality34.64 new3D/s는 특정 움직이는 장면·설정의60초 측정이다. 반복 게시 약60/s나 Quest 표시Hz를 새로운3D FPS로 표시하지 않는다. LAN·Quest·AV·장시간의 최소 성능 약속으로 사용하지 않는다.

## 저장소 구성과 개인정보

공개 저장소: README·LICENSE·고지·CONTRIBUTING·SECURITY·CHANGELOG, src, resources, native 소스/도구, 필요한 scripts, patches, tests, config/models.json, 공개 docs, .github.

개발 원본의 artifacts/models/.venv/.tools/실행 config/페어링/인증서/서명키/로그/캡처·소리/바로가기와 개인 실기 도구는 제외한다. 이번 감사에서 `.py`만 제외하던 배포 규칙이 실제 기기용 `.ps1`을 포함할 수 있음을 발견해 `*-reviewed.*`로 수정했다. 텍스트 내 키·토큰·현재 사용자 경로도 별도 검사한다. 패턴 통과는 모든 개인정보 부재의 증명이 아니므로 tar 내부·바이너리 metadata도 최종 검토한다.

```powershell
python scripts/release/export_repository.py --output artifacts/publication/new-review/repository
python scripts/release/export_repository.py --output artifacts/publication/new-review/repository --verify
```

기존 경로를 덮어쓰지 않으며 Git 초기화·push·로그인·업로드를 수행하지 않는다. 생성 후보의 manifest는 `native_corresponding_source_complete: false`를 유지한다. 원본 폴더에서 `git add .`를 실행하지 않는다. 공개 내용에 변경이 있으면 새 경로로 다시 export하고 최종 문서·소스·바이너리 해시를 동결한다.

## 출시 준비 순서와 완료 기준

내용 검사는 로컬 실행 계정의 Windows/WSL home 경로와 키·GitHub/AWS/Hugging Face/OpenAI 토큰 패턴을 검사한다. CI에서는 실행 계정이 달라 작성자의 개인 경로를 같은 방식으로 탐지하지 못한다. 현재 작성자 경로·기기 값은 별도 로컬 감사로 확인하고, 최종 export를 그 감사와 함께 보관한다. binary 자원의 metadata와 tar 내부는 이 텍스트 검사 범위 밖이다. 소스 감사 때 해시를 산출하고 복사한 바이트와 비교하며 변경 중인 파일은 공개 후보로 인정하지 않는다.

저장소 후보에는 기존 진단·파일 오디오 실험 소스도 일부 보존되어 있다. 제품 기능으로 안내하지 않으며 미사용 vendor·개인 실기 도구·비공개 연구 자료는 포함하지 않는다. 독립 유지보수가 시작되면 제품/실험 테스트를 분리하고 재현 가능한 fixture만 CI 대상으로 늘린다.

1. **소스 동결**: 최신 APK 전체 대응 소스 export, native·vendor 실제 구성 감사, host snapshot 복원. 깨끗한 폴더 재빌드와 라이선스 고지 완료
2. **설치 마감**: PC 버전 업데이트·바로가기 교체·설정/인증 보존·rollback·제거 구현과 실패 주입 검사. Quest 장기 서명/버전 고정, 동일 서명 업데이트 검증. DAD 설치도 GUI로 제공하면 명령 입력 부담을 줄일 수 있음
3. **새 설치/실기 검증**: Python 없는 Windows 사용자 또는 별도 PC에서 다운로드부터 준비·Scan·PIN·영상·2D/3D·Audio·종료·업데이트 확인. Quest2/3 최종 APK 착용·20회 재연결·60분 운영, GPU stage/p95/VRAM/drop/AV/누적 지연 기록. 불합격을 수치 변경으로 통과시키지 않음
4. **게시**: 저장소 owner/name·설명 확정, 안전한 export 검토, GPLv3·topic·Security/Issues 설정, 태그와 draft prerelease 생성, 해시·대응 소스·검증 요약 첨부. 최종 다운로드 링크로 새 설치 한 번 더 확인 후 공개

사용자가 지정한 GitHub 계정은 `myidwe`이며 직접 확인했다. 저장소 이름은 `Quest3D`를 제안했다. 게시할 소스·바이너리·검증 결과는 위 완료 기준에 맞춰 준비한다. 현재는 실제 설치·재빌드·로컬 Git 감사까지 진행하며 원격 생성·공개 게시는 아직 수행하지 않았다.

## Preview 이후 개선

PC와 Quest 설치를 안내하는 단일 시작 화면, 앱 안 DAD 다운로드, 검증된 GPU 아키텍처 확장, 진단 민감값 제거, 선택형 무료 가상 오디오 장치 경로를 순차 제공한다. 소유권 검사형 호환 업데이트/복원/보관 제거는 이번 준비에 구현했으며 공개 최종 묶음의 새 환경 검증을 남긴다. Quest 개발자 모드·헤드셋 USB 승인은 앱이 대신할 수 없다. 외부 드라이버를 검증 없이 동봉하거나 Steam 설치를 필수로 만들지 않는다.

[설치 안내](DISTRIBUTION.md) · [빌드](BUILDING.md) · [제품 범위](PRODUCT_SCOPE.md) · [의존성 감사](DEPENDENCY_AUDIT_2026-09-30.md)

## 이번 준비에서 수행한 작업

README의 Quest3 대기·옛 기능 상태를 수정하고 설치·지원·업데이트·제거·개발 입력을 정리했다. CONTRIBUTING·SECURITY·CHANGELOG·Issue 양식·최소 CI·안전한 repository export를 추가했다. 10개 Quest 테스트/build helper의 개인 home 기본 경로를 `$HOME`로 바꿨다. APK와 다른 버전의 소스 manifest를 함께 묶는 오류를 차단하고 audio probe 해시도 고정했다.

로컬 source/PC bundle은 **검토 후보**이며 공개하지 않았다. runtime·모델·Depth·사용자 설정과 설치된 Quest 앱은 변경하지 않았다. 새 PC 설치·소스 재빌드·서명 전환·최종 착용 검증은 이번 문서 검토 결과만으로 완료 처리하지 않는다. GitHub CI는 설정을 작성했으며 실제 GitHub 실행 결과는 아직 없다.
