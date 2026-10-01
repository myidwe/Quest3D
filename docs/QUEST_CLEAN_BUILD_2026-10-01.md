# Quest 공개 baseline 재빌드 — 검토안

2026-10-01. 기존 개발 APK·설치 앱·release pin을 보존하고, 새로운 unsigned 공개 package의 전체 Android 프로젝트를 실제로 빌드했다. 공개 게시·키 생성·서명·Quest 설치는 수행하지 않았다. 이 문서는 [9월 30일 소스·서명 검토](QUEST_RELEASE_PREPARATION_2026-09-30.md)의 후속 증거다.

## 확인 결과

| 단계 | 실제 결과 | 증거 |
|---|---|---|
| own stream·XR와 XR의 godot-cpp | 새 source에서 arm64 release 컴파일 성공, 344초 | `artifacts/publication/quest-clean-build-20260930-r2/native-build-summary.json` |
| 수정 Android Godot 엔진 | 고정 source + 8패치로 release 컴파일 성공, 1,228초 | 같은 디렉터리의 `engine-build-summary.json` |
| 전체 project·Java·DEX export | 새 import·GDScript 컴파일·Gradle release APK 성공, 563초 | `artifacts/publication/quest-android-export-20260930-r2/apk-export-template-stamp-fixed-summary.json` |
| APK↔새 native 출력 | 정확한 strip 후 4개 native의 SHA-256·ELF build ID 일치 | `artifacts/publication/quest-apk-correspondence-20261001-r2/apk-correspondence.json` |
| runtime Maven 목록 | 실제 `standardReleaseRuntimeClasspath`의 37개 artifact·35개 module 해시 확보 | export 디렉터리의 `dependency-audit-standard-release-summary.json` |
| Maven 고지 선언 | 35개 POM의 Apache-2.0 선언 확인, Guava는 parent POM까지 대조 | `artifacts/publication/quest-maven-notices-20261001-r2/maven-notices-review.json` |
| 도구 경계 테스트 | 61개 통과 | `artifacts/publication/quest-baseline-tests-20261001-e.xml` |

전체 export는 보존 APK의 compiled-script delta를 조립하지 않았다. GDScript 86개가 새 `.gdc`로 컴파일되었고, 현재 `GodotApp.java`·`CodecCapabilityDiagnostics.java`를 공식 template에 복사해 DEX를 생성했다. shader source는 project export에 포함했다. 이것이 헤드셋의 shader 실행·새 엔진 성능·음성을 실기에서 검증했다는 뜻은 아니다.

### 새 unsigned APK

| 항목 | 확인 값 |
|---|---|
| 경로 | `artifacts/publication/quest-android-export-20260930-r2/Quest3D-public-review-unsigned.apk` |
| SHA-256 | `a3e933791f9ee047619232ac56f0b350e1a0524151e13e8cd7437ede01bf54de` |
| 크기 | 118,807,824바이트 |
| package | `app.questto3d.client` |
| versionCode / versionName | `1` / `0.1.0-review` |
| min / target / compile SDK | `29` / `32` / `36` |
| debuggable | `false` — 실제 `aapt dump badging` 확인 |
| 서명 | unsigned — 실제 `apksigner verify`에서 서명 없음 확인 |
| 정렬 | `zipalign -c -P 16 -v 4` 통과 |
| ABI | `arm64-v8a` 하나, native 6개 |
| 실기·배포 | 미설치·미검증·미게시 |

APK 안 native는 Godot, stream, XR, OpenXR Vendors, Khronos loader, libc++다. Linux editor용 debug/release vendor native와 AAR가 APK assets·native에 잘못 포함되지 않았음을 검사했다. 새 APK에는 native 약 101.8MB와 DEX 약 5.1MB가 ZIP에서 압축되지 않은 구성이 포함된다. 기존 개발 APK는 128,394,802바이트이므로 새 APK 자체는 더 작다. Release ZIP의 압축 크기와 APK의 원래 크기를 비교하지 않는다. 엔진 feature·압축·크기 최적화는 실제 기능·설치·성능 회귀를 확인한 다음 별도로 결정한다.

새 engine·stream·XR raw SHA는 APK SHA와 직접 같지 않다. NDK `29.0.14206865`의 `llvm-strip --strip-unneeded`를 새 복사본에 적용한 결과가 APK와 정확히 일치했고 각 ELF build ID도 유지되었다. 원본과 strip 결과를 혼동하지 않는다. vendor는 공식 `5.0.0-stable` ZIP의 **release** arm64 입력을 같은 방식으로 대조했으며 이번에 자체 재컴파일하지 않았다. loader는 Maven `org.khronos.openxr:openxr_loader_for_android:1.1.54`의 AAR native와 byte 단위로 같다.

### 실제 권한

새 APK의 manifest에서 확인한 권한은 다음 6개다.

- `android.permission.INTERNET`
- `android.permission.CHANGE_WIFI_MULTICAST_STATE`
- `com.oculus.permission.HAND_TRACKING`
- `com.oculus.permission.RENDER_MODEL`
- `org.khronos.openxr.permission.OPENXR`
- `org.khronos.openxr.permission.OPENXR_SYSTEM`

마이크·카메라·외부 저장소 권한은 이 APK의 실제 목록에 없다. 이는 설치 시 Quest OS가 보여줄 모든 안내를 실기에서 확인했다는 의미가 아니다. 각 Quest OS의 동의·권한 표시와 연결은 설치 검증 때 확인한다.

## 대응 source와 수정 이유

새 source 검토 후보는 `artifacts/publication/quest-source-prep-20260930-r3/public-source`다. manifest 등록 파일 489개이며 manifest SHA-256은 `33d34c1f4926f4d74004dc00c11ba0cb96f6874d0be6e2aa151a04857fd2f060`이다. 기존 r2의 488파일 후보는 보존했다.

r2가 `bin` 디렉터리를 제외하면서 source인 `extensions/nightfall-xr/bin/nightfall-xr.gdextension`도 빠뜨렸음을 발견했다. 이전 정상 APK에 보존된 **plain-text source asset**의 정확한 bytes만 r3에 복원했다. source descriptor와 APK 원본 SHA·경로·길이를 provenance로 연결했으며 native 바이너리나 미확인 `.uid`를 복원하지 않았다. source collector는 이 정확한 descriptor 경로만 예외로 허용하고 다른 bin payload는 계속 제외한다.

native compile은 r2의 453개 project source를 사용했고, 전체 Android export는 descriptor가 추가된 r3의 454개 project source를 사용했다. descriptor 추가로 C++ 입력이 달라진 것은 아니다. `artifacts/publication/quest-source-correspondence-20261001.json`에서 두 후보의 공통 project source가 같은지, r3 manifest 전체 해시, 실제 patch 결과, export 이후 source 변경을 대조했다. public preset·Gradle loader overlay·Android template stamp는 후보에서 생성한 빌드 입력이며 해당 변경을 별도로 기록했다.

검증한 upstream은 Godot `5b4e0cb0fd279832bbdd69fed5354d4e5ad26f88` + 8패치, XR godot-cpp `05057de73de4b99f114d36c40d84ca46926c0e25`다. NDK `29.0.14206865`, SDK `36`, Build Tools `36.1.0`, Java `17.0.20.1+1`, CMake `3.31.6`, Ninja package `1.11.1.4`, SCons `4.9.1`을 유지했다. 13개 정적 링크 입력은 기존 고정 cache의 정확한 hash를 대조하고 사용했다. 새로운 결과·임시파일·Gradle cache는 A 드라이브에 생성했다.

공식 `Godot_v4.7-stable_export_templates.tpz` SHA-256 `9714459dc071907c0f3d5f17d608faf69e7cda21331fc5d39c4503ffa4e99eec`의 `templates/android_source.zip`이 실제 template SHA-256 `2dcb079f64b6cf9103cce273f42d1d5a4f52bc28d83a215579100fe568d6779c`과 같은 bytes임을 검사했다. 공식 Godot Java AAR를 normal dependency로 사용하고, 그 안의 엔진 runtime 두 파일은 이번 fresh build 결과로 교체했다.

## 발견한 실패와 보완

처음 Gradle inventory의 `releaseRuntimeClasspath`는 Godot의 flavor와 맞지 않았다. 실제 `standardReleaseRuntimeClasspath`로 수정해 성공했다. 첫 전체 export는 Android template 설치 stamp가 없어 77초 후 실패했고 실패 결과를 보존했다. 공식 설치 동작에 맞춘 `project/android/.build_version`의 `4.7.stable` stamp와 editor용 공식 Linux debug vendor input을 추가한 별도 receipt를 남긴 후 전체 export가 성공했다.

Godot exporter의 기본 loader 요청은 `1.1.53`이지만 기존 제품에서 확인한 loader는 `1.1.54`였다. 새 build recipe에 Gradle `strictly "1.1.54"` overlay를 넣어 정확한 loader를 유지한다.

Linux headless import는 Android용 own GDExtension을 Linux에서 로드할 수 없다는 메시지를 남겼다. 실제 Android native 6개와 script compile/export 성공을 별도로 검증했다. Android engine compile의 Swappy 미검출 경고는 기록했으며 실제 표시율·성능 영향은 미검증이다.

성공 export의 editor 종료에는 기존 기본 설정으로 ADB shutdown 연결 시도가 기록되었으나 daemon 연결은 실패했다. 설치나 ADB 장비 조작을 수행하지 않았다. 이후 helper에 `shutdown_adb_on_exit=false`를 추가했다. 이 한 줄은 성공 export **이후** 추가한 예방 설정이며 해당 export로 실행 검증했다고 표시하지 않는다.

Windows의 global Git `core.autocrlf`가 patch 결과 bytes에 영향을 주는 테스트 실패도 해결했다. 최종 prepare helper는 inherited `GIT_*`·global/system Git 설정을 격리하고 LF patch 적용을 고정한다. 실제 r2 준비는 이 helper 보완 이전에 수행되었으므로 이전 실행에 최종 helper SHA를 소급해서 붙이지 않는다. source/patch 입력·실제 native 출력 대응과 실행 단계별 receipt를 사용한다.

## 의존성 고지와 남은 조건

35개 Maven POM의 Apache-2.0 선언을 확인했다고 전체 APK 고지 완료로 판정하지 않는다. LICENSE 전문·요구되는 NOTICE/copyright·정적 링크 source/recipe와 native/vendor 고지의 실제 배포 구성이 남아 있다. stream의 FFmpeg는 `gpl` feature가 켜져 있으므로 APK 전체를 MIT/Apache만으로 설명하지 않는다.

OpenXR Vendors `5.0.0-stable`의 exact commit `6a04c8632140f7dc14670e5564fd473464047a15`에서 build·license·release recipe 359파일, 9,654,671바이트를 제한된 다운로드로 수집하고 Git blob identity와 SHA-256을 모두 검사했다. 대형 sample media가 포함된 전체 archive는 64MiB 제한에서 중단되어 사용하지 않았다. vendor의 두 source submodule은 아직 수집·재빌드하지 않았다.

[공식 release workflow](https://github.com/GodotVR/godot_openxr_vendors/blob/6a04c8632140f7dc14670e5564fd473464047a15/.github/workflows/build-addon-on-push.yml)는 Meta OpenXR mobile SDK v77의 preview headers를 `meta_headers`로 넣고 [SConstruct](https://github.com/GodotVR/godot_openxr_vendors/blob/6a04c8632140f7dc14670e5564fd473464047a15/SConstruct)는 `META_HEADERS_ENABLED`를 설정한다. 따라서 Meta SDK 활성 입력이 없다고 단정하지 않는다. 기존 SDK·loader 고지를 보존하고 정확한 header 출처·라이선스 제공 방식의 검토를 계속한다. Khronos loader가 Apache-2.0이라는 확인과 이 vendor header 검토는 별개다.

**공개 승격의 필수 미완료**는 완전한 dependency source/recipe·LICENSE/NOTICE 제공, 최종 public version/source provenance 확정, 별도 release key·실제 서명, 깨끗한 사용자 설치·같은 서명 업데이트, Quest 2/3에서 연결·Scan·2D/3D·화면·음성·장시간 회귀다. 현재 `source_complete`, `dependency_notices_verified`, `public_release_ready`를 true로 바꾸지 않았다. historical r2의 gate도 보존한다. 이번 신규 native/engine/full export의 성공은 별도 증거로 기록한다.

**추가 재현 범위**는 compatible editor/API generator 자체와 untouched 공식 Java AAR/vendor·정적 라이브러리의 전량 source 재컴파일이다. 검증한 공식 dependency의 version/hash/source/license를 고정해서 사용하는 것과 모든 untouched dependency를 재컴파일해야 한다는 조건을 혼동하지 않는다. 다만 source·고지 제공의 실질적 누락은 필수 미완료에 남긴다.

## 다음 실행 방법

`scripts/release/prepare_quest_build_baseline.py` → `rebuild_quest_baseline.sh` → `rebuild_quest_engine.sh` → `prepare_quest_android_export.py` → `audit_quest_gradle_dependencies.sh` → `export_quest_unsigned_baseline.sh` → `verify_quest_unsigned_apk.py` 순서다. 모든 output에는 **존재하지 않는 새 A 드라이브 경로**를 사용한다. 기존 성공 output을 덮어쓰거나 실패 receipt를 제거해 재시도하지 않는다.

아래는 이미 검증된 pin·SDK·editor·정적 dependency cache를 준비한 WSL에서의 명령 구조다. `QUEST_REVIEW_TOOLS`는 그 고정 tool cache 경로다. 깨끗한 clone의 모든 입력을 자동 bootstrap하는 단일 명령이 완성되었다고 표시하지 않는다.

```bash
cd /mnt/a/ai/quest_to_3d
python3 scripts/release/prepare_quest_build_baseline.py --source artifacts/publication/quest-source-prep-20260930-r3/public-source --output artifacts/publication/quest-new-native
bash scripts/release/rebuild_quest_baseline.sh artifacts/publication/quest-new-native "$QUEST_REVIEW_TOOLS" 6
bash scripts/release/rebuild_quest_engine.sh artifacts/publication/quest-new-native "$QUEST_REVIEW_TOOLS" 6
python3 scripts/release/prepare_quest_android_export.py --source artifacts/publication/quest-source-prep-20260930-r3/public-source --native artifacts/publication/quest-new-native --vendor .tools/quest/downloads/godotopenxrvendorsaddon.zip --template "$QUEST_REVIEW_TOOLS/godot-templates/templates/android_source.zip" --output artifacts/publication/quest-new-export
bash scripts/release/audit_quest_gradle_dependencies.sh artifacts/publication/quest-new-export "$QUEST_REVIEW_TOOLS"
bash scripts/release/export_quest_unsigned_baseline.sh artifacts/publication/quest-new-export artifacts/publication/quest-new-native "$QUEST_REVIEW_TOOLS" first
python3 scripts/release/verify_quest_unsigned_apk.py --export artifacts/publication/quest-new-export --rebuilt artifacts/publication/quest-new-native --vendor .tools/quest/downloads/godotopenxrvendorsaddon.zip --tools "$QUEST_REVIEW_TOOLS" --output artifacts/publication/quest-new-verification --attempt first
```

helper는 실제 키·서명·Quest 설치·GitHub 게시를 수행하지 않는다. signed public build와 source manifest를 확정할 때 [기존 서명 절차](QUEST_RELEASE_PREPARATION_2026-09-30.md#공개-apk와-서명-경계)의 audit gate를 통과시켜야 한다. 새 unsigned APK를 기존 정상 개발 APK나 최종 배포 묶음으로 자동 교체하지 않는다.

위 명령은 역사적 공식 vendor 기반 baseline이다. 후속 공개 후보에서는 `collect_quest_vendor_baseline.py`로 고정 vendor/submodule source를 준비하고 `rebuild_quest_public_vendor.sh`로 arm64 native를 빌드한다. `prepare_quest_android_export.py`와 `verify_quest_unsigned_apk.py` 모두 `--public-vendor <새 vendor build>`를 사용한다. 최종 dependency source/고지는 `collect_quest_release_dependencies.py` → `complete_quest_source_supply.py`가 새 source manifest에 결합한다. 실제 공개 후보 receipt와 아래의 AAR native 대응 검사를 함께 사용한다.

## 공개 후보 후속 작업 — 2026-10-01

GitHub 공개 준비를 이어 진행하면서 기존 r3·a3 APK·review-e 묶음을 보존한 새 후보를 만들고 있다. 아래 새 결과를 앞의 역사적 결과에 소급해 붙이지 않는다.

OpenXR Vendors는 같은 commit `6a04c8632140f7dc14670e5564fd473464047a15`에서 **공개 Khronos header만 선택하여 새 arm64 release native를 빌드했다**. `meta_headers`를 지정하지 않았으며 122초에 성공했다. 실제 raw native SHA-256은 `4eeb9d10e9e1de2432e4deac8da309534ad3ac1abb621994cc138bc5aa777a0e`다. godot-cpp submodule은 `58d1de720b8ffe9f8ffcdfe3a85148582cfd2e74`, OpenXR source submodule은 `ba4aec9686cb94c99a55f7ceba9768e9e35525c2`로 고정하고 공식 source archive를 수집했다. 해당 빌드에서는 Meta preview calibration/fidelity·boundary visibility·stationary preview가 제외된다. 현재 앱의 source에서 이 preview API 호출은 발견되지 않았으며, 일반 stereo·controller·화면·stream 기능의 실기 회귀는 별도 확인한다.

`artifacts/publication/quest-release-dependencies-20261001-r5/dependency-source-notices.json`은 실제 native 8개 package의 ABI에 기록된 port·patch input과 source archive 9개의 원래 fetch SHA-512를 대조했다. 버전이 같은 최신 port로 바꾸지 않고 실제 registry cache의 정확한 recipe를 공급한다. Moonlight의 custom nested header, vcpkg 도구 source, FFmpeg의 GPL-enabled 실제 configure flags를 함께 남긴다. 실제 runtime Maven 37개 artifact의 SHA를 재검사했고 35개 module의 published source JAR을 모두 수집했다. POM 선언뿐 아니라 runtime/source archive의 LICENSE·NOTICE와 Apache-2.0 전문을 제공한다.

신규 helper·기존 export/signing 경계 테스트 52개가 통과했다. 여기에는 archive 경로/링크 거부, 기존 source·key 보존, public vendor native 변조 거부, nested NOTICE 보존, 실제 Windows current-user DPAPI roundtrip이 포함된다. 외부 private release key는 source와 ZIP 밖에 생성했다. 공개 인증서 SHA-256은 `d950d11633753a3a52acba925dff8a35ccc0df7de1aef77291a1868c93f73cfc`다. private key·암호·DPAPI 파일은 배포 자료에 넣지 않는다.

새 public version은 `app.questto3d.client`, versionCode `1`, `0.1.0-preview`다. 앞선 미서명 a3 및 r3 gate를 수정하는 방식으로 공개 후보를 만들지 않는다. 모든 native/DEX/payload와 source supply를 검증한 **별도 manifest**를 서명한다. 현재 신규 APK의 서명·실기·게시 완료를 이 단락만으로 판정하지 않는다.

첫 `quest-public-export-20261001` 전체 export는 600초에 성공했으나 실제 native 검증에서 탈락했다. SHA-256 `ce1733eb548e19a56a39fa05c5f20b93229ed54de65c1c7c9bd6ff6f1e3a1a8c` APK에는 교체한 GDExtension `.so` 대신 공식 AAR 안의 이전 vendor native가 들어갔다. 이 APK는 공개 후보로 승격하거나 서명하지 않는다. 실패 APK와 검증 receipt를 보존한다.

export helper는 새 public vendor를 `.so`와 Android AAR **양쪽**에 넣도록 수정했다. 원래 AAR는 별도로 보존하며 Java·resource entry의 bytes를 유지하고 선택하지 않은 ABI만 제외한다. 실제 APK의 vendor가 NDK strip 결과 `95ee3eda182999bcfe66a3faac472fdfa399078f79ddaf2915ffaa922fff9e38`과 같아야 한다. `quest-public-export-20261001-r2`에서 새 전체 export를 진행한다. 설정값만 보고 성공 판정하지 않고 APK bytes를 대조한다.

## 새 공개 source 검증 완료 — 2026-10-01

수정한 `quest-public-export-20261001-r2` 전체 export는 **566초**에 성공했다. unsigned APK는 118,824,200바이트, SHA-256 `54e6a461db54b6e8eb164b73c7f1643a2d2f648ff409e175c80eec359a674702`다. 실제 vendor native는 위의 `95ee3eda…f9e38`과 정확히 같고, own stream/XR·수정 Godot·libc++·loader를 포함한 native 6개 대응, unsigned, 16KB ZIP 정렬을 모두 검사했다. package/version/권한은 `quest-public-apk-verification-20261001-r2/apk-correspondence.json`의 실제 APK 조회 결과다.

새 대응 source는 `artifacts/publication/quest-public-source-20261001-r4/public-source`이며 manifest 1,134파일, SHA-256 `dc26d3c4cdc7c069d25676504d030ad541f94d5e3936eb1a389d7deb7373f57f`다. 실제 새 APK와 exact source/recipe/dependency supply 대조를 통과해 이 **별도 후보**의 `source_complete`, `clean_build_verified`, `dependency_notices_verified`가 true다. 역사적 r3/a3/개발 APK의 false gate는 그대로 보존한다. binary 배포에 필요한 65개 `binary_notice_files`는 `NOTICES.md`와 source manifest에 해시가 기록된 `licenses/` 텍스트다. root binary bundle은 이 bytes를 함께 공급한다.

`PUBLIC_HEADER_INPUT_AUDIT.json`은 공급 source archive 15개의 실제 SHA·멤버 수를 기록한다. Meta mobile SDK·meta_headers·openxr_preview.h의 실제 archive/header 경로는 0개이며 공식 vendor ZIP/AAR의 옛 native는 이 source에 들어 있지 않다. 과거 `meta/LICENSE-SDK`의 3줄 저작권·SDK URL 참조는 역사적 텍스트로 구분해 보존한다. 공개 source에 SDK의 사용하지 않는 proprietary header/archive를 추가하지 않는다. 이 입력 검증은 exact pinned source/build hash를 보완하며 파일 이름 검색만으로 모든 라이선스를 판정한다는 주장은 하지 않는다.

원본 curl archive의 `docs/examples/usercertinmem.c`와 OpenSSL archive의 sample/test 288파일에는 공개 fixture private-key marker가 있다. 원 archive SHA와 실제 vcpkg fetch SHA-512를 대조한 upstream source이며 프로젝트의 private release key 예외와 구분한다. marker 내용을 출력하거나 새 private key를 source에 공급하지 않는다. 별도 evidence는 `quest-public-upstream-fixture-audit-20261001.json`이다.

이 source input 단계의 focused 테스트는 56개 통과, 1.82초(`quest-public-release-tests-20261001-inputaudit.xml`)다. 새 proprietary preview header가 source archive에 들어오는 실패와 역사적 SDK URL 텍스트 보존을 함께 검증한다. 이후 실제 WSL 서명 pipe 경계까지 포함한 최종 focused 결과는 60개 통과, 1.73초다. 실기·전체 공개 준비 상태는 이 source/build 검증과 분리하여 계속 기록한다.

## 실제 공개 서명 확정 — 2026-10-01

`artifacts/publication/quest-public-signed-20261001-r2/Quest3D-Quest.apk`의 실제 공개 서명이 완료됐다. 크기 118,866,943바이트, SHA-256 `dd4ce7cf7d9438e6dd3f96b13f2a405ac70dae32c138c64c64416760bc7fb232`다. `app.questto3d.client` / versionCode `1` / `0.1.0-preview`, release 인증서 SHA-256 `d950d11633753a3a52acba925dff8a35ccc0df7de1aef77291a1868c93f73cfc`로 실제 검증했다. 16KB 정렬을 통과했고 JAR 서명 metadata를 제외한 제품 ZIP entry 400개의 해시가 unsigned snapshot과 모두 같았다.

signed APK의 대응 source는 같은 디렉터리의 `corresponding-source`다. 최종 manifest SHA-256은 `7615aea4d809a218c2aba6a706eecbef5afb97d289fc777282b33a3ece384419`이며 원 unsigned APK/source manifest hash, 실제 공개 package/version/cert, 최종 signed APK hash를 연결한다. 원 r4 source는 보존했다. 모든 1,134파일의 최종 manifest 대조도 통과했다. `signing-result.json`이 이 결과의 receipt다.

첫 WSL `getpass` 대기 output은 보존하고 실제 private pipe 서명으로 별도 r2를 만들었다. 비밀번호·keystore bytes·DPAPI payload·private key 경로는 source, APK, 공개 보고서에 포함하지 않는다. 설치·동일 서명 업데이트·헤드셋 연결/화질/음성 결과는 root의 실제 설치 회귀 보고서에서 확인하며 서명 성공으로 대체하지 않는다. 이 source manifest의 `hardware_verified`와 `public_release_ready`는 서명 당시의 미검증 상태를 그대로 기록한다.
