# Quest3D 빌드·재현

0.1.1-preview의 stream은 개인정보 경로 제거를 위해 정적 의존성 13개와 함께 다시 빌드한다. `prepare_quest_private_path_build.py`, `rebuild_quest_private_path_native.sh`, `collect_quest_private_native_proof.py`와 대응 소스의 privacy-native 기록을 따른다. OpenSSL 빌드 정보 소스 패치를 포함하며, 이전 캐시를 재사용하는 `rebuild_quest_baseline.sh`만 실행해 새 stream이 재현되었다고 판단하지 않는다. 최종 signed APK와 ZIP은 `privacy_audit.py`로 검사하고 source·서명·입력 해시 검증도 별도로 완료한다.


## 0.1.0-preview 공개 빌드 — 2026-10-01

사용자 설치는 [DISTRIBUTION](DISTRIBUTION.md)을 따른다. GitHub 저장소는 제품 코드·설치 도구·검사를 제공한다. native fork와 의존성의 완전한 입력은 **같은 Release의 Quest3D-Source ZIP**으로 제공한다. 자동 `Source code.zip`만으로 전체 앱이 빌드되는 것은 아니다. 빌드 도구와 모델 다운로드도 필요하다.

현재 제품은 monitor 확대 보기와 **frame bridge protocol 2**다. PC 입력·내장 파일 플레이어·선택 영역만 입체화·protocol 3은 지원 범위가 아니다. TensorRT·학습·대형 모델 실험이 현재 제품 런타임을 뜻하지 않는다.

### 새 공개 호스트

새 `sunshine.exe` SHA는 `86eb2ee5e3177a892f15ecd5ba869b27d3e1b9131848b1adacf9a25301767d42`다. 완전한 선택 소스·고정 dependency archives·14개 정적 라이브러리 source packages·원문 고지·도구가 `sources/sunshine`에 있다. `SOURCE.md`와 `provenance.json`을 먼저 읽는다.

```powershell
python scripts/release/unpack_host_release.py --supply <SourceZIP폴더>/sources/sunshine --output <새빌드폴더>
```

고정 MSYS2 UCRT64 도구와 Windows npm을 준비한 뒤 UCRT64 bash에서:

```bash
export QUEST3D_NATIVE_NPM='<고정 Node의 npm.cmd 경로>'
bash <SourceZIP폴더>/sources/sunshine/tools/rebuild_host_release.sh <새빌드폴더> 6
```

공개 unpacker는 tar의 모든 member SHA·경로·dependency SHA를 확인한다. 실제 새 추출에서 CMake configure도 통과했다. 재빌드 도구는 native/CPU 회귀/web을 빌드하며 서버·서비스·방화벽을 자동 실행하지 않는다. source package의 원 recipe/checksum은 함께 제공되지만 모든 MSYS2 recipe를 다시 실행했다는 뜻은 아니다. 빌드 머신 차이로 바이너리 SHA가 달라질 수 있으며 동작 검증을 별도로 수행한다.

기존 개발 `77c950b5…`의 역사 source snapshot은 packet API 누락으로 재빌드에 실패했다. 공개판은 완전한 수정 소스로 새 호스트를 빌드했다. 기존 바이너리의 누락 원문을 복구했다고 주장하지 않는다. [전체 증거·제외 근거](HOST_RELEASE_PREPARATION_2026-09-30.md)

### 공개 Quest

`sources/quest/SOURCE_BUILD_NOTES.md`, `source-manifest.json`, `NOTICES.md`와 dependency-supply 기록을 따른다. 새 project·Godot engine·stream/XR·OpenXR vendor를 빌드하고 APK 안의 **6개 native 라이브러리와 loader**를 실제 입력과 비교한다. 공식 addon AAR가 다른 native 파일을 우선 패키징할 수 있으므로 빌드 성공만으로 대응을 판단하지 않는다. 공개 vendor는 고정 Khronos 공개 헤더를 사용하며 Meta preview proprietary 헤더를 운영 입력으로 요구하지 않는다.

`prepare_quest_android_export.py`, `export_quest_unsigned_baseline.sh`, `verify_quest_unsigned_apk.py`, `complete_quest_source_supply.py`가 고정 export·Maven SHA·실제 APK·source/notice gate를 검증한다. 자세한 사용 도구·명령·실패 기록은 [Quest clean build](QUEST_CLEAN_BUILD_2026-10-01.md)에 있다.

새 공개 export에는 준비·APK 검사 양쪽에 **`--public-vendor <공개 vendor 빌드 폴더>`**를 지정한다. 이 입력은 직접 `.so`와 파생 AAR의 native를 함께 고정한다. `.so`만 교체하거나 아래의 역사적 공식 addon 명령을 그대로 사용하는 것은 새 공개판 재현 경로가 아니다.

공개 패키지는 **`app.questto3d.client`**, 개발 패키지는 `app.questto3d.client.debug`다. `sign_quest_release.py`는 non-debug 공개 unsigned APK·exact source·인증서 fingerprint를 확인하고 외부 장기키로 서명한다. 개인키/암호는 저장소·ZIP·argv·로그에 넣지 않는다. 본인 fork는 별도 패키지·본인 키를 사용한다. 공개 앱 업데이트는 동일 키/패키지와 증가한 versionCode가 필요하다. 소유자는 복구 가능한 외부 키 백업을 유지한다.

### 같은 버전 배포 묶음

EXE 설치본은 ZIP 검증 후 [EXE 빌드 절차](EXE_INSTALLERS.md)를 따른다. C# 소스·권한 manifest·footer parser가 저장소에 포함되며 Windows의 .NET Framework C# 컴파일러를 사용한다. 0.1.2 설치 묶음은 기존 개인정보 수정 Quest APK 0.1.1-preview/code3을 그대로 사용한다. 설치 묶음 버전과 APK versionCode를 혼동하지 않는다.

```powershell
python scripts/release/build_bundle.py --output <새출력폴더> --release 0.1.2-preview --sources --exe-installers --host-release <검토한HOST_RELEASE.json> --quest-source <서명된대응소스폴더> --quest-apk <서명된APK> --quest-sha256 <실제APK해시>
python scripts/release/review_assets.py --directory <새출력폴더> --release 0.1.2-preview --output <새검증JSON>
```

새 호스트는 `--host-release`와 대응 소스를 함께 지정한다. 기본값은 역사 후보 검토용이며 새 공개판 선택을 대신하지 않는다. 각 ZIP의 actual size/SHA, PC↔host source, APK↔Quest source/package/서명, binary↔source 원문 고지를 대조한다. 이 도구는 업로드하지 않는다. `ready_for_public_release: false`는 모든 실기를 완료한 정식판이 아니라는 뜻이며 제한을 명시한 Preview 공개 판단과 구분한다.

GPU 없는 Windows 검사는 `.github/workflows/source-checks.yml`의 고정 pytest/psutil/NumPy와 exact 파일 목록을 사용한다. 제품 소스 import용 **`PYTHONPATH=src`**를 설정한다. CUDA/QML·실제 캡처·제어 ACK·Quest 수신·착용·음성·장시간은 별도 증거다. 같은 PC 격리 설치를 새 Windows 설치로 보고하지 않는다. Release의 `release-validation.json`에 완료·미검증 범위를 기록한다.

## 이전 개발 경로와 준비 기록

**이 아래는 2026-09-30의 개발/조사 기록이다.** 당시 미완료 gate, 기존 77 호스트·debug APK와 명령을 새 공개판의 상태/재현 명령으로 사용하지 않는다. 새 공개판은 위의 exact source와 검증 기록을 따른다.

현재 `.tools`, `third_party`, `vendor`, `artifacts`는 개발 PC의 준비된 입력이다. GitHub 소스만 받는 유지보수자에게 자동으로 제공되지 않는다. 깨끗한 clone에서 최신 PC+Quest APK까지 빌드하는 단일 명령은 아직 없다. 고정 입력·fork 소스·서명·출시 검증을 준비한 다음 공개 재현 가능으로 표시한다.

## 제품과 소스 경계

| 부분 | 위치·역할 |
|---|---|
| PC | `src/quest3d`, `resources/desktop`, `resources/ui` — PySide6/QML, 캡처·깊이·좌우 합성·UI |
| 호스트 | Sunshine 고정 upstream와 수정 소스 — BGRA bridge, NVENC·네트워크·오디오 |
| 캡처 | `native/capture`와 고정 `wc_cuda` — Windows GPU 캡처·HDR 어댑터 |
| Quest | Nightfall/Godot/Moonlight/OpenXR 수정 소스 — 디코딩·눈별 렌더·배치·설정 |
| 설치 | `scripts/release`, `native/host/*installed*.ps1` — 고정 의존성·검증·로컬 인증 |

현재 PC 앱은 CUDA graph를 사용한다. 실험 TensorRT·대형 모델·학습 후보가 현재 제품 런타임이라고 해석하지 않는다. Quest PC 입력·내장 파일 플레이어는 현재 제품에서 노출하지 않는다. 실험 소스가 존재한다고 지원 기능이 되는 것은 아니다.

## 고정 입력

| 입력 | 현재 고정값·정의 |
|---|---|
| Python | `3.12.6 x64`, `.python-version` |
| PC 의존성 | `pyproject.toml`, `uv.lock`; Torch `2.7.1+cu126`, torchvision `0.22.1+cu126`, PySide6-Essentials/shiboken6/Qt `6.8.3`, safetensors `0.6.2` |
| 모델 | `config/models.json`의 repository·revision·파일 SHA-256. DAv2 Small 기본, DAD Small 선택 |
| Sunshine | `cb72dffa3233c5815cd5ba88f09f049dd679ba75` |
| Host 도구 | `native/host/toolchain.lock.json`; MSYS2 UCRT64, Node `24.20.0`, 개별 archive SHA-256 |
| wc_cuda | `6f6c6eaed91f36f0e937f1da92cf5cfc35a9bfcc`; Rust `1.88.0`, maturin `1.9.4`, `Cargo.hdr.lock` |
| Nightfall | `2c2162af9738dadb32e441a48255cef65bf7dd56` |
| Godot / godot-cpp | `5b4e0cb0fd279832bbdd69fed5354d4e5ad26f88` / `05057de73de4b99f114d36c40d84ca46926c0e25` |
| Quest 도구 | `scripts/quest/versions.lock.json`, `downloads.lock.json`; NDK `29.0.14206865`, SDK `36`, build-tools `36.1.0`, CMake `3.31.6`, SCons `4.9.1` |

라이선스·SHA-256은 [제3자 고지](../THIRD_PARTY_NOTICES.md)와 릴리스 manifest도 확인한다. 모델의 다른 크기에 Small의 라이선스를 적용하지 않는다. 잠금 파일을 임의 최신화하지 않는다.

## PC Python 환경

Windows x64, Git, 고정 Python·uv와 NVIDIA Turing sm75 GPU가 필요한 경로다. 일반 CPU 로직은 GPU 없이 검사할 수 있지만 전체 의존성은 현재 Windows lock과 GPU 패키지에 묶여 있다.

`uv.lock`은 `vendor/wheels/wc_cuda-0.1.2+quest1-cp310-abi3-win_amd64.whl`을 로컬 입력으로 참조한다. 이를 재빌드하거나 검증된 릴리스에서 확보하지 않으면 소스의 `uv sync`가 완성되지 않는다. 운영 HDR 캡처는 별도 `+quest2` 경로를 먼저 로드한다. `+quest3` 창 캡처 실험은 선택하지 않는다.

고정 wheel·도구·모델 소스를 준비한 개발 checkout에서:

```powershell
uv sync --locked --extra gpu-capture
.\.venv\Scripts\python.exe -m quest3d.cli setup-model
.\.venv\Scripts\python.exe scripts/release/verify_installed_gpu.py --report artifacts/build/gpu-check.json
.\.venv\Scripts\python.exe scripts/release/verify_installed_ui.py --root . --report artifacts/build/ui-check.json
```

`setup-model`은 고정 가중치를 다운로드하며 DAD는 `--model-id distill_any_depth_small`로 추가한다. `verified_model_source`를 만족할 고정 Depth Anything V2 추론 소스도 준비해야 한다. 설치 묶음은 이 소스·manifest를 포함해 최종 사용자에게 Git을 요구하지 않는다.

GPU 검사는 작은 합성 입력으로 동봉 CUDA 소스를 컴파일·실행한다. UI 검사는 실제 QML·폰트·아이콘을 offscreen 렌더한다. 실제 캡처·Quest 표시·착용·FPS·음성을 대신하지 않는다.

## Sunshine 호스트

현재 개발 fork의 준비·빌드 경로다. 기존 수정 checkout·다른 commit·patch 충돌을 강제로 초기화하지 않는다.

```powershell
.\native\host\prepare-source.ps1
.\native\host\bootstrap-host.ps1
.\scripts\build-host.ps1 -Mode bridge
.\scripts\build-host.ps1 -Mode all -Jobs 8
.\native\host\test-host.ps1
```

Bootstrap은 프로젝트 안 MSYS2/Node에 고정 archive를 복원한다. 출력은 `third_party/sunshine/cmake-build-quest3d`다. 현재 host 회귀·singleton 검사는 이후 기능을 포함하므로 역사적 운영 바이너리의 소스 검사와 동일하지 않다.

운영 `sunshine.exe` SHA-256은 `77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63`이다. 대응 소스 후보는 `sources/sunshine/upstream.tar`에 `quest3d-host-20260909-2253.tar`를 적용하고 `provenance.json`의 submodule archive를 기록 위치에 복원하는 구조다. 현재 변경된 `third_party/sunshine`를 그대로 이 바이너리의 대응 소스라고 표시하지 않는다.

**역사적 바이너리와 스냅샷의 깨끗한 재빌드 대응성은 미검증**이다. 공개 전에 고정 입력·실제 소스·빌드 명령으로 재빌드하고 bridge/NVENC·새 Pair·영상·소리를 확인한다. 비트 단위 SHA 일치만으로 판단하지 않으며 실제 변경을 모두 포함하는 소스인지와 동작 대응 증거를 남긴다. 대응 소스 미완료의 바이너리를 완성된 공개판으로 표시하지 않는다.

## GPU 캡처 wheel

Windows Visual Studio C++ build tools가 필요하다. 프로젝트 전용 Rust·maturin을 사용한다. 고정 upstream에 `quest1` 패치, HDR에서는 추가 `quest2` 패치·`Cargo.hdr.lock`을 적용한다.

```powershell
.\native\capture\build.ps1 -Bootstrap -TestNative
.\native\capture\build.ps1 -ExperimentalHdr -TestNative
```

출력은 `artifacts/capture`, `artifacts/capture/hdr-experimental`이다. 다른 checkout·Cargo.lock은 덮어쓰지 않는다. 처음에는 큰 도구 다운로드가 필요할 수 있다. 운영 `+quest2` wheel SHA-256은 `0d8bea69406e930dc424694061ed109489cc230b6948721e18ec5838505a4496`이다. 새 hash는 원인·라이선스·동작 검증 후 배포 pin을 갱신하며 기존 운영 환경에 자동 적용하지 않는다.

## Quest native와 APK

기본 스크립트는 WSL `Ubuntu-20.04`를 사용한다. Git/curl/tar/python3/dpkg-deb/gcc/g++/make/rg와 고정 Android·Godot·vcpkg 입력이 필요하다.

```powershell
.\scripts\build-quest.ps1 -Stage Status
.\scripts\build-quest.ps1 -Stage Prepare -Distribution Ubuntu-20.04
.\scripts\build-quest.ps1 -Stage Native -Distribution Ubuntu-20.04
.\scripts\build-quest.ps1 -Stage Stream -Distribution Ubuntu-20.04
.\scripts\build-quest.ps1 -Stage Xr -Distribution Ubuntu-20.04
.\scripts\build-quest.ps1 -Stage Scripts -Distribution Ubuntu-20.04
.\scripts\build-quest.ps1 -Stage Tls -Distribution Ubuntu-20.04
.\scripts\build-quest.ps1 -Stage Identity -Distribution Ubuntu-20.04
.\scripts\build-quest.ps1 -Stage Apk -Distribution Ubuntu-20.04
.\scripts\build-quest.ps1 -Stage Verify -Distribution Ubuntu-20.04
```

이는 기본 고정 빌드의 단계 목록이다. **이 명령만으로 2026-09-30 모든 누적 변경을 깨끗하게 재현했다고 주장하지 않는다.** 최신 앱은 baseline APK/native를 재사용해 검토된 UI delta를 조립한 이력이 있다.

캐시는 기본 WSL 사용자 home 안에 있다. `bootstrap.py`는 그 밖의 `QUEST_BUILD_CACHE`를 거부하므로 `-CachePath /mnt/a/...`만 지정하면 실패한다. A 드라이브 위주 사용은 WSL 배포판 저장 위치나 검증된 캐시 정책으로 해결해야 한다. 현재 안전 검사를 제거하지 않는다. 결과 APK·Windows 자료는 프로젝트 `artifacts/quest`에 저장한다.

최신 Quest 입력:

- source: `artifacts/quest/level-settings-20260930/project`
- overlay: `patches/quest-level-settings-20260930`와 이전의 고정 baseline/overlay
- APK: `artifacts/quest/level-settings-20260930/build/candidate-signed.apk`
- SHA-256: `81a9fc0f3008e95bbdb10f17d0421bd364475ee532cb14341e9d3007803f5042`

`scripts/prepare-quest-level-build.py --output <새 artifacts/quest 폴더>`는 보존된 compact/display/pointer baseline을 요구한다. APK 조립 도구도 baseline APK·export 결과를 요구한다. 누적 변경 검증 도구이며 소스만 받는 사용자의 완전한 bootstrap을 대신하지 않는다.

2026-09-30 추가 준비에서 `prepare_quest_source.py`로 최신 APK에 연결된 공개용 `source-manifest.json`을 생성했다. 전체 project와 보존 source archive·정적 의존성 입력을 수집하고 실제 mDNS native 수정 소스 3개를 맞췄다. 이는 **소스 검토 후보**이며 `source_complete: false`, `clean_build_verified: false`다. 최신 script, Nightfall stream/XR, Godot, Moonlight, OpenXR vendor addon·Android 입력의 전체 깨끗한 재빌드·고지를 확인해야 한다. 서명된 APK의 무결성만으로 대응 소스가 완성되는 것은 아니다.

`build_apk.sh`는 프로젝트 도구 폴더에 **debug keystore**를 만들며 공개 서명 흐름이 아니다. 공개 패키지·장기 서명 키·비공개 보관·이전 앱 마이그레이션을 먼저 확정한다. keystore·개인 인증서·ADB 키·헤드셋 설정은 저장소/ZIP에서 제외한다. 실기 설치는 [Meta 기기 설정](https://developers.meta.com/vr/documentation/native/android/mobile-device-setup/)과 [Google ADB](https://developer.android.com/tools/adb)를 따른다.

2026-10-01에는 별도 소스 r3에서 새 Android engine·stream/XR 컴파일과 전체 project/Java/DEX export가 성공했다. 실제 unsigned public APK의 native/loader/manifest 대조와 사용한 고정 도구, 재현 명령, 실패 기록은 [Quest clean build 검토](QUEST_CLEAN_BUILD_2026-10-01.md)에 있다. 전체 dependency 고지·source 공급·서명·사용자 설치·Quest 실기는 아직 완료되지 않았다. 아래 개발 APK·역사 snapshot 기준과 새 빌드 증거를 혼용하지 않는다.

`scripts/release/sign_quest_release.py`는 별도로 빌드한 `app.questto3d.client`·non-debug unsigned APK만 대상으로 한다. 정확한 소스·unsigned APK·package/version·예상 인증서 해시를 검증한 뒤 외부 private key로 서명한다. 비밀번호는 대화·명령 인수·보고서 대신 apksigner stdin으로 전달한다. 현재 개발 APK를 다시 서명해 공개판으로 바꾸거나 키를 자동 생성·기기에 설치하지 않는다.

## 역사적 host 소스 재현 결과

`prepare_host_source.py`와 `rebuild_host_source.sh`는 기존 dirty checkout을 수정하지 않고 upstream pin + 역사적 49파일 overlay + 필요한 Windows submodule을 새 A 드라이브 경로에 조립했다. 구성 단계는 통과했으나 `src/stream.cpp:1879`에서 빌드가 실패했다. snapshot의 `audio::packet_t`는 struct인 반면 upstream stream.cpp는 tuple `std::get`을 사용한다. 역사 snapshot에 필요한 변경이 일부 누락되어 있다는 실제 증거다.

따라서 기존 source ZIP의 역사 snapshot은 완성된 재빌드 소스가 아니다. 당시 누락 파일을 복원하거나, 완전한 소스로 새 release host를 빌드하고 영상·오디오·페어링·프로토콜을 재검증해야 한다. 그 전까지 기존 pinned 실행 파일을 공개 대응 소스 완료로 표시하지 않는다. 기존 동작 runtime은 교체하지 않았다.

## 배포 묶음

`scripts/release/build_bundle.py`는 허용된 소스·자원만 수집하며 기존 출력 폴더를 덮어쓰지 않는다. 고정 도구·바이너리·최신 대응 소스 manifest를 준비한 뒤 새 폴더에서 실행한다.

```powershell
.\.venv\Scripts\python.exe scripts/release/build_bundle.py --help
.\.venv\Scripts\python.exe scripts/release/build_bundle.py --output artifacts/releases/public-candidate --release 0.1.0-preview --sources
```

Quest까지 묶으려면 `--quest-source <검토된 manifest 폴더> --quest-apk <검토된 APK> --quest-sha256 <APK 해시>`를 추가한다. APK는 `--sources`와 함께만 묶는다. 이 명령은 공개 업로드하지 않으며 개인 개발 폴더 전체를 묶는 도구가 아니다.

`review_assets.py --directory <묶음 폴더> --release <같은 버전> --output <새 보고서>`는 ZIP 내부 manifest/모든 파일 해시, PC host와 source, Quest APK와 source/package/version/인증서 기록의 연결을 대조한다. 성공은 archive 무결성·연결 검증이며 새 설치·고지·전체 native 소스·실기 승격을 의미하지 않는다. 보고서는 공개 준비 미완료를 유지한다.

추출한 PC 후보의 다음 검사는 설치·다운로드 없이 수행한다.

```powershell
powershell -NoProfile -File .\scripts\release\install.ps1 -VerifyOnly
```

source ZIP은 외부 source archive를 포함하고 저장소 자동 Source code ZIP과 다를 수 있다. Release 실행 파일 옆에 **그 버전의 완전한 대응 소스**를 연결한다. 자동 Source code ZIP만으로 누적 fork/native 입력 제공을 완료했다고 판단하지 않는다.

## CI와 출시 검증

GPU 없는 CI는 경로·archive·manifest·설치 정책·모델 pin·일반 Python 로직·공개 파일 검사를 담당한다. Windows 전용 검사는 Windows runner, `gpu` / `capture` 검사는 물리 장비에서 실행한다. private artifacts에 의존하는 검사를 무조건 전체 실행하거나 skip을 하드웨어 검증 성공으로 세지 않는다.

Host/native/APK 재현과 Quest 착용은 CI 설계만으로 검증되지 않는다. 공개 승격에는 다음을 별도 기록한다.

1. 깨끗한 소스의 PC/host/capture/Quest 빌드·고지·대응 소스 검토.
2. 기존 개발 도구 없는 PC의 Python 실제 설치·다운로드·CUDA·QML·바로가기·방화벽.
3. Scan/주소·새 PIN·실제 캡처→AI→양안→NVENC→Quest 2/3 영상.
4. 2D/3D·Depth·모델/AI Quality·Display·Sound·설정 보존·중지·재시작.
5. 새 3D FPS와 반복 송출 분리, VRAM·드롭·누적 지연·음성 오차·장시간.
6. 같은 서명 APK 업데이트, PC 업데이트 정책, 제거·방화벽·오디오 원복.

환경·입력 pin·실패/미검증 상태를 `release-validation.json`에 남긴다. 기존 개발 설치 정상 동작을 새 설치 완료나 모든 GPU 지원 근거로 사용하지 않는다.
