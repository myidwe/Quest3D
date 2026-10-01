# Quest 공개 소스·서명 준비 — 검토안

검토일: 2026-09-30. 현재 정상 동작 개발용 앱을 보존하면서 공개용 대응 소스와 서명 절차를 준비했다. GitHub 게시, release 키 생성, 실제 release 서명, Quest 설치는 이 작업에서 수행하지 않았다.

후속: [2026-10-01 clean build 검토](QUEST_CLEAN_BUILD_2026-10-01.md)에서 별도 r3 source 후보와 수정 Android 엔진·stream·XR·전체 Java/DEX unsigned export를 실제 빌드했다. 아래 r2·개발 APK의 역사적 결과와 gate는 그대로 보존하며 새 후보의 성공·미완료는 후속 문서로 구분한다.

## 현재 판정

공개 소스 검토 후보는 `artifacts/publication/quest-source-prep-20260930-r2/public-source`이다. manifest에 등록된 파일은 488개, 합계 493,200,796바이트이며 대부분의 용량은 upstream 소스 archive이다. `source-manifest.json` SHA-256은 `160a5fbc53ec906f714f93a0192a9cfc2893b5823bc7d56f94fefe6152c90b5d`이다. 전체 파일 해시와 manifest 밖 추가 파일 검사를 통과했다.

이 후보는 **검토 자료이며 완성된 공개 릴리스가 아니다**. `source_complete`, `clean_build_verified`, `dependency_notices_verified`, `public_release_ready`는 모두 `false`이다. 자료를 수집하거나 부분 해시가 일치했다는 이유로 이 값을 바꾸지 않는다. 실제 새 환경 빌드와 의존성 검토가 완료된 뒤 그 결과에 따라 갱신한다.

보존한 이전 484파일 후보 `artifacts/publication/quest-source-prep-20260930/public-source`도 수정하지 않았다. r2는 누락된 vendor 고지 2파일, 공식 MIT 고지, native 대조 증거를 추가한 별도 디렉터리다. 모델 가중치, 페어링, 사용자 설정, 개인키, 캡처 영상과 실행 바이너리는 소스 후보에 포함하지 않았다. export preset의 keystore 경로·사용자·비밀번호는 후보 복사본에서 비웠다.

## 정확히 대응하는 현재 APK

| 항목 | 확인 값 |
|---|---|
| APK | `artifacts/quest/level-settings-20260930/build/candidate-signed.apk` |
| SHA-256 | `81a9fc0f3008e95bbdb10f17d0421bd364475ee532cb14341e9d3007803f5042` |
| package | `app.questto3d.client.debug` |
| versionCode / versionName | `1` / `1.0.0` |
| 개발용 인증서 SHA-256 | `a0c962041dd1a2154f07ffb5dd44867f8789d721a876c4540e2d9b70a9809dee` |
| minSdk / targetSdk / compileSdk | `29` / `32` / `36` |
| 서명 검증 | APK v3 서명 검증 통과, 개발용 identity |

인증서 해시는 공개된 인증서의 식별자다. 개인키나 비밀번호를 수집·복사했다는 뜻이 아니다. 현재 APK와 장비의 설치 앱·설정·페어링은 변경하지 않았다.

## 소스 복원과 실제 입력 대조

최신 보존 project의 stream 소스는 mDNS 수정 이전 상태였다. 해당 85파일은 당시 baseline 해시와 일치했지만, 실제 APK의 stream native에는 2026-09-14 검색 수정이 들어 있었다. 후보에서만 `mdns_browser.cpp`, `mdns_browser.h`, `mdns_parser.h`를 보존된 native 검증 기록의 정확한 3파일 해시에 맞춰 복원했다. 현재 working tree 또는 원 APK를 덮어쓰지 않았다.

최근 GDScript·shader 8파일은 export-sources 기록과 대조했다. 이것은 선택된 변경 파일의 대응 확인이며, 모든 compiled script·Android DEX·native를 소스에서 다시 만든 전체 clean build 증거는 아니다.

APK의 arm64 native 6개는 아래 실제 입력까지 추적했다. 전체 SHA-256은 후보의 `NATIVE_INPUT_VERIFICATION.json`과 `SOURCE_PROVENANCE.json`에 기록했다.

| APK native | 실제 입력과 확인 결과 | 남은 범위 |
|---|---|---|
| `libc++_shared.so` | NDK `29.0.14206865` sysroot의 raw 파일을 `llvm-strip --strip-unneeded`로 새 복사본에 처리한 결과가 APK와 일치 | 전체 Android toolchain 고지·배포 범위 최종 검토 |
| `libgodot_android.so` | 보존된 수정 Godot `4.7.stable` debug arm64 template에 같은 strip을 적용한 결과가 APK와 일치 | 엔진 pin·8패치로 새 환경 재빌드 |
| `libgodotopenxrvendors.so` | 공식 `5.0.0-stable` ZIP의 Android debug arm64 입력에 같은 strip을 적용한 결과가 APK와 일치 | vendor 소스 빌드·실제 활성 SDK 구성 검토 |
| `libnightfall-stream.android.template_release.arm64.so` | 9월 14일 보존 native 해시와 일치. 당시 정적 링크 입력 13개와 source baseline·mDNS 복원을 대조 | 전체 stream 새 빌드와 링크 provenance 재검증 |
| `libnightfall-xr.android.template_debug.arm64.so` | 최신 보존 project native와 APK가 일치하며 native 소스를 포함 | XR 새 빌드 및 Android 연결 검증 |
| `libopenxr_loader.so` | Maven `org.khronos.openxr:openxr_loader_for_android:1.1.54` AAR의 arm64 native와 정확히 일치 | 나머지 Gradle·DEX 의존성 목록 완성 |

OpenXR loader의 확인된 출처는 Khronos이며 Apache-2.0이다. AAR SHA-256은 `66702fc24475d7a5362688e77444670124c2fa119745d14c2524bcf3eb584834`이다. 이는 Meta 독점 loader를 포함했다는 이전 추정을 해소한다. [정확한 Maven artifact](https://repo.maven.apache.org/maven2/org/khronos/openxr/openxr_loader_for_android/1.1.54/)를 기록했다.

OpenXR Vendors ZIP SHA-256은 `b68135657f64fea782cac3efe3e5cef7a4de13e4215f1b621ce6a49fe32592eb`이며, tag의 소스 commit은 `6a04c8632140f7dc14670e5564fd473464047a15`이다. 본체의 MIT 고지를 확보했고 raw ZIP native와 stripped APK native의 차이를 구분했다. 기록의 `native_matches_apk: false`는 raw 비교이고 `stripped_matches_apk: true`는 동일 strip 후 비교다. [공식 release ZIP](https://github.com/GodotVR/godot_openxr_vendors/releases/download/5.0.0-stable/godotopenxrvendorsaddon.zip), [해당 commit의 LICENSE](https://raw.githubusercontent.com/GodotVR/godot_openxr_vendors/6a04c8632140f7dc14670e5564fd473464047a15/LICENSE).

addon의 `meta/LICENSE-SDK`와 `LICENSE-LOADER`도 후보에 보존했다. 파일 이름만으로 독점 SDK가 실제 실행 코드에 포함되었다고 결론 내리지 않는다. 반대로 loader가 Khronos임을 확인했다고 APK 전체가 MIT 또는 Apache만으로 구성되었다고 판단하지 않는다. 활성 vendor 소스·헤더·AAR·DEX의 실제 구성과 각 고지 검토가 남아 있다.

## 빌드 pin과 정적 의존성

| 구성 | pin |
|---|---|
| Nightfall upstream | `2c2162af9738dadb32e441a48255cef65bf7dd56` |
| Godot upstream | `5b4e0cb0fd279832bbdd69fed5354d4e5ad26f88` + 엔진 패치 8개 |
| XR의 godot-cpp | `05057de73de4b99f114d36c40d84ca46926c0e25` |
| stream의 godot-cpp | vcpkg `4.4`, `godot-4.4-stable` source archive |
| vcpkg tool / registry baseline | `f781d9387e4684783e69e136e2e124ff4660bffc` / `026ac03d99ca88e45b5f260267b22e103ec05ec0` |
| NDK / SDK / Build Tools | `29.0.14206865` / `36` / `36.1.0` |
| CMake / Ninja / SCons | `3.31.6` / `1.11.1.4` / `4.9.1` |

XR와 stream의 godot-cpp pin은 서로 다르다. 하나의 공통 최신 버전으로 설명하지 않는다.

실제 Linux 빌드 cache의 `avcodec`, `avformat`, `avutil`, `crypto`, `curl`, `enet`, `godot-cpp`, `moonlight-common-c`, `opus`, `ssl`, `swresample`, `swscale`, `z` 정적 라이브러리 13개는 당시 링크 입력 해시와 모두 일치했다. 대응 cache의 패키지 목록은 FFmpeg `7.1.2`, curl `8.17.0`, OpenSSL `3.6.0`, Opus `1.5.2`, zlib `1.3.1`, SIMDe `0.8.2`, godot-cpp `4.4`이다. Quest FFmpeg의 확인된 feature 목록에는 x264/x265가 없다. Windows Sunshine의 FFmpeg 구성과 혼동하지 않는다.

Moonlight custom port는 `7b026e77be62175104640e7e722b758df6d3d0d7`, ENet는 `dea6fb5414b180908b58c0293c831105b5d124dd`를 사용하며 custom portfile·패치·Quest 헤더를 포함했다. 실제 cache에서 9개 정적 의존성 source archive, Godot/godot-cpp Git archive, 8개 installed copyright와 NDK 고지를 새 후보에 수집했다. archive가 존재하거나 정적 library 해시가 일치하는 것만으로 원본 fetch recipe의 SHA-512·전처리·configure·재빌드가 모두 검증되었다고 판단하지 않는다.

현재 보존 build-tools는 역사적 입력이다. 독립 새 환경에서 이 후보만으로 전체 APK를 만드는 하나의 확정된 빌드 명령은 아직 검증되지 않았다.

## 도구와 검증 명령

`scripts/release/prepare_quest_source.py`는 새 후보 디렉터리만 생성하고 원 project·APK·기존 후보를 보존한다. 실제 source hash, 정확한 APK native 목록, mDNS 복원 기록, 파일 경로·symlink/junction·개인 파일 제외를 검사한다. 기존 디렉터리에는 출력하지 않는다. 기본 project 수집과 실제 Linux cache의 archive·고지 수집을 구분한다.

Windows 프로젝트 루트에서 현재 후보를 확인하는 명령:

```powershell
.venv\Scripts\python.exe scripts/release/prepare_quest_source.py --verify artifacts/publication/quest-source-prep-20260930-r2/public-source
```

이미 준비된 Linux 빌드 cache가 있는 경우 새로운 수집 후보를 만드는 예시:

```bash
python scripts/release/prepare_quest_source.py \
  --output artifacts/publication/quest-source-new-review \
  --build-cache "$QUEST_BUILD_CACHE" \
  --include-native-source-archives \
  --native-evidence artifacts/publication/quest-source-prep-20260930
```

`QUEST_BUILD_CACHE`는 사용자가 가진 실제 cache 경로다. Windows와 Linux에서 해당 경로 접근·toolchain을 확인해야 한다. 이 명령은 소스 수집이며 APK 빌드·설치 명령이 아니다. Git archive 수집은 기존 객체를 사용하며 implicit fetch를 금지한다. 새 다운로드는 별도 검증된 준비 절차에서 수행한다.

## 공개 APK와 서명 경계

제안된 공개 package는 `app.questto3d.client`이다. 현재 debug package와 다른 새 public build를 만든다. 기존 개발 키를 공개 release identity로 사용하지 않으며, 이 작업에서 새 키를 생성하지 않았다. 공개 키 선택·보관·백업은 실제 release identity 확정 단계에서 수행한다.

개발 앱과 공개 앱은 병행 설치할 수 있게 준비하고 기존 앱을 자동 삭제하지 않는다. package가 다르므로 공개 앱의 첫 실행에는 새 서버 페어링과 설정이 필요할 수 있다. 첫 공개판 이후에는 같은 package·같은 release key·증가한 versionCode로 업데이트하고, 설정·페어링 보존을 실제 확인한다. 사용자가 자체 소스 빌드를 서명할 때는 자신의 key를 사용하며 공식 release와 서명 identity가 다름을 안내한다.

`scripts/release/sign_quest_release.py`의 기본 동작은 audit뿐이다. `--execute`가 있어야 서명하며 개인키 생성·package 변경·설치·게시 기능은 없다. 현재 debug APK를 release로 다시 서명하는 경로는 거부한다.

spec은 **실제로 새로 빌드한 unsigned public APK**와 대응 source manifest의 SHA-256, package, versionCode, versionName, 별도 release 인증서 SHA-256, `signing_kind: release`를 기록한다. 아래는 필드 구조이며 `ACTUAL_*`는 실제 측정값으로 대체해야 한다.

```json
{
  "unsigned_apk_sha256": "ACTUAL_UNSIGNED_APK_SHA256",
  "source_manifest_sha256": "ACTUAL_SOURCE_MANIFEST_SHA256",
  "apk_metadata": {
    "package": "app.questto3d.client",
    "version_code": 1,
    "version_name": "0.1.0-preview",
    "certificate_sha256": "ACTUAL_RELEASE_CERTIFICATE_SHA256",
    "signing_kind": "release"
  }
}
```

audit 명령 구조:

```powershell
python scripts/release/sign_quest_release.py --spec <spec.json> --unsigned-apk <public-unsigned.apk> --source <corresponding-source> --aapt2 <aapt2.exe>
```

도구는 실제 APK의 package·version·debuggable, source 전체 파일 해시와 APK 연결을 검사한다. `source_complete`, `clean_build_verified`, `dependency_notices_verified`가 true가 아니면 실제 서명을 거부한다. 이 세 조건을 통과해도 프로젝트 전체 공개 readiness·실기·개인정보 검증은 별도로 통과해야 한다.

서명 단계는 다음 순서를 사용한다.

1. audit 이후 원 APK가 바뀌지 않았는지 해시 재검사
2. 새 출력 디렉터리에 `audited-unsigned.apk` snapshot을 만들고 같은 해시 재검사
3. snapshot의 모든 제품 ZIP entry 해시 기록 후 zipalign, 16KB alignment 확인
4. 명시한 JDK·Build Tools와 외부 keystore로 서명. 비밀번호는 대화형 또는 명시적 private pipe 입력 후 apksigner stdin으로만 전달
5. 실제 서명 인증서·alignment 확인. 서명 metadata만 제외하고 모든 제품 ZIP entry가 snapshot과 동일한지 검사
6. 원 source manifest가 audit 이후 바뀌지 않았는지 확인하고 별도 `corresponding-source` 복사본 생성
7. 복사본 manifest를 최종 signed APK 해시에 연결. 원 unsigned APK 해시·원 source manifest 해시·실제 인증서·package·version도 기록
8. 검증을 모두 통과한 결과만 `Quest3D-Quest.apk`로 확정

원 source manifest는 변경하지 않는다. source가 서명 도중 바뀌거나 제품 ZIP payload가 달라지면 `signed-unverified.apk`를 최종 APK로 확정하지 않는다. 비밀번호는 명령 인자·환경변수·결과 JSON에 기록하지 않는다. Python 문자열의 메모리 완전 삭제까지 보장한다고 주장하지 않는다. 서명 작업은 별도 격리된 프로세스에서 실행한다.

zipalign을 서명 전에 수행하고 `apksigner`로 검증하는 순서는 [공식 Android Build Tools 안내](https://developer.android.com/tools/apksigner)를 따른다. 실제 release key를 사용한 서명·설치·업데이트는 아직 미검증이다.

## 도구 테스트와 남은 gate

`tests/test_quest_release_source.py`의 26개 테스트가 통과했다. source/APK 보존, 정확한 mDNS 복원, 경로·개인 파일 제외, manifest 변조·누락·추가, debug identity 거부, 미완성 source 서명 거부, 비밀번호 stdin, audit 이후 APK 변경, unsigned→signed source 연결, 서명 metadata 이외 payload 변경 거부를 확인했다.

결과: `artifacts/publication/quest-source-test-results-20260930-audit-race.xml`. Android 서명 subprocess는 통제된 stub으로 검증했으며, 실제 release 서명 성공 또는 Quest 실기 검증으로 표현하지 않는다.

공개 APK 배포 전 남은 작업:

- 이 후보에 포함한 pin·patch·dependency archive로 독립 새 환경에서 Godot·stream·XR·Android export 전체 빌드
- 모든 GDScript·shader·DEX·native의 최종 빌드 입력과 선택 과정을 연결하고 compiled-script delta 조립에 의존하지 않는 공개 baseline 확정
- archive fetch recipe 해시와 custom port 전처리·configure·link 입력 대조
- 실제 Android/Maven/vendor 및 정적 의존성 목록과 고지·대응 소스 제공 방식 완료
- 공개 package·version 정책 및 별도 release identity 확정, 실제 서명·설치·동일 서명 업데이트 보존 확인
- Quest 2·Quest 3에서 연결·2D/3D·화면 설정·오디오·재연결과 기존 성능/화질 회귀 검증
- 최종 PC·Quest·Source ZIP에서 개인 자료·개인키·모델·Steam/NVIDIA 드라이버 미포함 재검사

bit-for-bit 재빌드 자체를 GPL의 일반적 필수 조건으로 해석하지 않는다. 그러나 알려진 입력에서 빌드가 실패하거나 필요한 소스·변경이 빠진 상태는 대응 소스 검토의 실제 미완료 항목이다. 부분적인 native hash 일치는 이 남은 조건을 대체하지 않는다.

## Release identity 후속 — 2026-10-01

위의 9월 30일 결과는 보존했다. 공개 준비 후속 단계에서 별도 public package를 위한 새 PKCS12 release identity를 생성했다. 공개 인증서 SHA-256은 `d950d11633753a3a52acba925dff8a35ccc0df7de1aef77291a1868c93f73cfc`, RSA 3072비트, 유효기간 10,000일이다. 생성/인증서 확인의 암호는 stdin으로만 전달했고 소스·명령 인자·환경 변수·보고서에는 기록하지 않았다. 기존 debug key와 설치 앱은 변경하지 않았다.

유지관리자의 private key는 checkout·artifacts·배포 묶음 밖에 둔다. 현재 Windows 사용자와 SYSTEM으로 파일 ACL을 제한하고, 무작위 암호는 Windows current-user DPAPI로 저장했다. DPAPI 복구를 실제 확인했지만 **외부 백업은 수행하지 않았다**. DPAPI 파일은 생성한 Windows 계정/장치에 의존하므로 다른 PC에 그 파일만 복사하는 것은 암호 백업이 아니다. 유지관리자는 PKCS12와 암호를 자신의 안전한 비밀번호 관리자/암호화된 백업에 각각 보관해야 한다. 실제 private 경로는 공개 문서에 기록하지 않고 유지관리자에게 별도로 전달한다.

`scripts/release/windows_release_identity.py`는 기본으로 공개 fingerprint만 표시한다. `--show-password`는 유지관리자가 로컬 콘솔에서 명시적으로 실행하는 암호 복구용이며 공개 CI·로그에 사용하지 않는다. 최초 public release 이후 업데이트에는 같은 package/인증서와 증가한 versionCode를 사용한다. 자체 빌드 사용자의 개인 서명 identity는 공식 release와 별개다.

같은 도구의 `--sign-spec` 경로는 DPAPI 암호를 메모리에서 복구하고 기존 `sign_quest_release.py`의 stdin으로만 전달한다. 정확한 인증서가 다른 spec은 암호 복구 전에 거부하며 raw tool stderr는 공개 출력에 포함하지 않는다. source·build·NOTICE audit과 signed APK의 인증서/payload 검증을 건너뛰지 않는다. `--unsigned-apk`, `--source`, `--output`, `--linux-tools`를 명시하며 출력은 존재하지 않는 새 디렉터리를 사용한다.

신규 release boundary와 기존 source/export/signing 회귀 54개가 통과했다(`quest-public-release-tests-20261001-final-r2.xml`). AAR 안의 vendor native도 교체되는지, Java fixture bytes와 원 AAR가 보존되는지, 암호가 argv·환경·결과에 노출되지 않는지 확인한다. 실제 공개 서명과 설치 여부는 최종 서명 receipt·실기 보고서로 별도로 확정한다.

실제 Windows→WSL 실행에서는 `getpass`가 전달한 pipe 대신 `/dev/tty`를 열어 대기하는 동작을 발견했다. 첫 unsigned snapshot/aligned output은 보존하고 해당 서명 프로세스만 종료했다. signer의 `--passwords-from-stdin`을 wrapper에서 명시하도록 수정했다. interactive terminal은 이 모드에서 거부하며 newline으로 끝나는 제한 길이의 두 줄만 받는다. argv·환경에는 암호를 넣지 않는다. 정상 대화형 `getpass` 경로는 유지한다.

이 수정과 source preview-header 경계를 포함한 최종 focused 회귀는 60개 통과, 1.73초(`quest-public-release-tests-20261001-pipe.xml`)다. 새 `quest-public-signed-20261001-r2`에서 실제 서명을 재실행한다. 첫 실패 output과 원 unsigned source manifest를 덮어쓰지 않는다.

## 실제 release 서명 완료 — 2026-10-01

최종 `artifacts/publication/quest-public-signed-20261001-r2/Quest3D-Quest.apk`는 실제 public release 인증서로 서명·검증했다. SHA-256 `dd4ce7cf7d9438e6dd3f96b13f2a405ac70dae32c138c64c64416760bc7fb232`, 118,866,943바이트, `app.questto3d.client` / versionCode `1` / `0.1.0-preview`다. 실제 cert는 위의 `d950d116…f73cfc`이며 unsigned input의 제품 ZIP entry 400개가 모두 같은 해시를 유지했다. 16KB alignment와 최종 대응 source full manifest를 검사했다.

대응 `corresponding-source/source-manifest.json` SHA-256은 `7615aea4d809a218c2aba6a706eecbef5afb97d289fc777282b33a3ece384419`다. 원 unsigned source manifest `dc26d3c4cdc7c069d25676504d030ad541f94d5e3936eb1a389d7deb7373f57f`를 보존하고 별도 signed copy의 APK metadata/hash만 최종 서명본에 연결했다. `signing-result.json`에서 정확한 signed·unsigned APK hash와 source binding을 확인할 수 있다.

완료한 것은 source/build/NOTICE 공급과 실제 공개 서명이다. Quest 2/3 실기, 설치/같은 서명 업데이트, Scan·연결·양안·음성·장시간 회귀, 최종 ZIP과 GitHub 게시 여부는 각 실제 실행 기록으로 구분한다. private identity의 외부 백업은 여전히 수행하지 않았으므로 유지관리자가 자신의 안전한 백업을 준비해야 한다.
