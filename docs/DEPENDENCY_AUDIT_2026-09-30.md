# 의존성·배포 소스 감사 — 공개 준비안

검토일: 2026-09-30. 실제 GitHub 게시·바이너리 배포는 아직 수행하지 않았다. 이 문서는 확인한 파일과 공식 약관을 구분하며, 모든 재배포 의무가 충족되었다는 선언은 아니다.

## 추가 실측 결과

역사적 Sunshine 소스를 새 A 드라이브 경로에 조립하고 공식 pin/overlay/Windows submodule의 25,047파일을 독립 대조했다. missing/extra/changed는 모두0이었다. 그러나 CMake configure 후 실제 compile은 `src/stream.cpp:1879`의 tuple 접근과 snapshot `audio::packet_t` struct 불일치로 실패했다. archive 정확성과 전체 대응 소스의 완전성은 별개이며, 누락된 변경 파일 복원 또는 새 공개 host baseline 재빌드가 필요하다.

최신 Quest 공개 소스 r2 manifest는 488파일을 수집했고 원 APK 해시는 유지했다. project에 빠져 있던 실제 mDNS 수정 3파일은 보존된 검증 해시로 맞췄다. Godot·NDK libc++·공식 OpenXR Vendors native를 새 복사본에 동일 strip 옵션으로 처리하면 APK native와 정확히 일치했으며 vcpkg 정적 라이브러리 13개도 당시 링크 입력과 일치했다. 전체 APK clean build·모든 vendor 및 Android 의존성 고지 완료는 여전히 false다. 정확한 후보 경로·핀·서명 준비·남은 gate는 [Quest 공개 준비 검토안](QUEST_RELEASE_PREPARATION_2026-09-30.md)에 기록했다.

실제 APK의 OpenXR loader 해시는 Maven `org.khronos.openxr:openxr_loader_for_android:1.1.54` AAR의 native와 정확히 일치했다. loader는 확인된 Khronos Apache-2.0 입력이다. Meta LICENSE 파일의 존재만으로 독점 loader를 포함했다고 판단하지 않는다. OpenXR Vendors의 공식 입력 대조는 완료했으며 vendor 내부 구성·나머지 Android/DEX 의존성 검토는 별도로 남아 있다.

## 판단

Quest3D 자체 코드는 현재 `LICENSE`의 GPLv3로 공개하는 경로가 적합하다. Sunshine·Nightfall의 GPLv3 수정본을 포함하므로 전체를 MIT로 표시하는 방식은 적합하지 않다. 각 외부 구성요소는 원래 라이선스와 저작권을 유지한다. 비용 없이 사용할 수 있는 구성과 모든 실행 의존성이 오픈소스라는 주장은 구분해야 한다. NVIDIA 드라이버·CUDA 및 선택적 Steam 오디오 드라이버는 별도 약관의 대상이다.

현재 배포 후보 빌더는 모델·개인 설정·페어링 자료·서명 개인키를 제외하고, 파일 해시와 대응 소스를 함께 묶는 방향으로 구성되어 있다. 다만 최신 Quest APK의 완전한 대응 소스 묶음, 새 환경의 빌드 절차, 모든 APK 의존성 고지가 아직 완료되지 않았다. 기존 정상 동작 개발용 APK를 그대로 공개 릴리스로 승격하지 않는다.

## 확인한 핀과 라이선스

| 구성요소 | 실제 핀·사용 범위 | 확인 결과 |
|---|---|---|
| Sunshine | `cb72dffa3233c5815cd5ba88f09f049dd679ba75` + Quest3D 수정 | GPLv3. 고정 실행 파일에 대한 역사적 수정 소스와 upstream/submodule tar를 보존 |
| Nightfall | `2c2162af9738dadb32e441a48255cef65bf7dd56` + Quest3D 수정 | GPLv3. 최신 설치 앱은 후속 수정 소스·native 변경을 함께 추적해야 함 |
| Godot / godot-cpp | `5b4e0cb0fd279832bbdd69fed5354d4e5ad26f88` / `05057de73de4b99f114d36c40d84ca46926c0e25` | MIT 본체 + 각 내장 의존성 고지. 수정된 엔진 패치도 빌드 입력에 포함해야 함 |
| Godot OpenXR Vendors | `5.0.0-stable`, addon ZIP SHA-256 `b68135657f64fea782cac3efe3e5cef7a4de13e4215f1b621ce6a49fe32592eb` | 본체 MIT, vendor 구성요소는 별도 약관. Meta 관련 확인 범위는 아래에 구분 |
| wc_cuda | `6f6c6eaed91f36f0e937f1da92cf5cfc35a9bfcc` + quest1/quest2 패치 | MIT. 수정 소스·Cargo lock·원본 라이선스 포함 경로 존재 |
| Depth Anything V2 Small | 가중치 revision `03876f8651c73a60fe4c2c48294e09fcb6838fcf`; SHA-256 `715fade13be8f229f8a70cc02066f656f2423a59effd0579197bbf57860e1378` | 정확한 모델 카드의 Apache-2.0 확인. 가중치는 설치 시 다운로드, 소스 ZIP에 제외 |
| Distill Any Depth Small | `xingyang1/Distill-Any-Depth`, revision `38095a41cca1e28a28e8bb6372c68df721455a2d`, `small/model.safetensors`; SHA-256 `56a173c0e1b5045bf6296a5c1fb16eace0bbde2eddc24b37532cb1774ac09caa` | 정확한 revision의 원본 README `license: apache-2.0` 확인. 선택 다운로드. 학습 코드 MIT와 가중치 라이선스를 혼동하지 않음 |
| DAD 학습 코드 | `6d8f415392eafb49c96a38cc4dedbd09a1607f50` | 공식 저장소 MIT. 현재 런타임은 DAv2 DPT 구현을 재사용하므로 이 학습 코드를 묶지 않음 |
| PySide6-Essentials / shiboken6 | `6.8.3`, `uv.lock`의 Windows wheel 해시 | Community LGPLv3/GPLv3 계열. 현재 installer는 공식 wheel을 일반 venv에 설치하고 메타데이터·license를 유지하며 라이브러리 교체를 막지 않음 |
| Python / PyTorch / torchvision | `3.12.6` / `2.7.1+cu126` / `0.22.1+cu126` | 각각 원래 약관 유지. Torch wheel의 CUDA·기타 내장 구성요소까지 프로젝트 GPL로 재표시하지 않음 |
| Pretendard / Lucide | `v1.3.9` / `0.468.0` | OFL-1.1 / ISC 및 Feather 고지. 리소스 매니페스트와 라이선스 파일 존재 |

DAv2의 Base/Large/Giant 또는 다른 모델 가중치에 Small의 Apache-2.0 판단을 자동 적용하지 않는다. 다른 모델·정밀도 엔진·CUDA 패키지를 추가할 때 해당 파일의 원본 라이선스와 다운로드 출처를 별도로 확인한다.

공식 근거: [DAv2 Small의 정확한 카드](https://huggingface.co/depth-anything/Depth-Anything-V2-Small/blob/03876f8651c73a60fe4c2c48294e09fcb6838fcf/README.md), [DAD의 정확한 원본 카드](https://huggingface.co/xingyang1/Distill-Any-Depth/raw/38095a41cca1e28a28e8bb6372c68df721455a2d/README.md), [DAD 학습 코드 LICENSE](https://raw.githubusercontent.com/Westlake-AGI-Lab/Distill-Any-Depth/6d8f415392eafb49c96a38cc4dedbd09a1607f50/LICENSE), [Qt for Python 6.8 Community 안내](https://doc.qt.io/qtforpython-6.8/commercial/index.html), [Godot OpenXR Vendors의 라이선스 범위](https://github.com/GodotVR/godot_openxr_vendors#license).

## Sunshine 대응 소스

`scripts/release/build_bundle.py`는 실행 파일 SHA-256 `77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63`을 고정한다. 소스 묶음에는 upstream tar, 2026-09-09 22:53의 수정 소스 49개, 해당 submodule tar, toolchain lock을 포함한다. 이 역사적 소스는 이후 실험용 working tree와 구분되어 있다.

빌더의 provenance에는 `binary_source_rebuild_verified: false`가 기록되어 있다. 공개용 검증은 이 소스를 새 디렉터리에 복원하고 실제 빌드한 뒤, 프로토콜·영상·오디오 동작이 배포 바이너리와 대응하는지 확인해야 한다. GPL 대응 소스 확인은 단순한 파일 보유보다 넓은 문제이며, bit-for-bit 해시 일치 자체를 GPL의 일반적 필수 조건으로 해석하지 않는다. 빌드 시각·경로 차이로 바이트가 달라도 설명 가능한 빌드 입력과 동작 대응 증거가 필요하다.

현재 `native/host/prepare-source.ps1`은 working tree용 `patches/sunshine`을 적용하고, `build-host.sh`도 현재 checkout을 빌드한다. 따라서 공개 소스의 역사적 snapshot 복원 절차가 없으면 이 두 스크립트만으로 고정 바이너리의 대응 소스 재빌드를 안내할 수 없다. 보존 snapshot 기반의 별도 재빌드 절차 또는 공개용 새 host 빌드·핀 갱신이 필요하다.

기존 source candidate의 Windows에 필요한 Sunshine submodule에는 FFmpeg와 x264/x265 등 소스 archive가 포함되어 있다. 모든 `source_included: false`를 위반으로 판단하지 않는다. Linux Flatpak·macOS 관련 자료처럼 Windows 실행 파일 빌드에 쓰지 않는 항목과, 실제 링크된 라이브러리를 구분해 완전성을 확인한다. FFmpeg 사전 빌드 입력의 정확한 릴리스·해시·빌드 설정도 추적한다.

## 최신 Quest APK의 실제 구성

검토한 2026-09-30 APK에는 아래 6개 arm64 native 라이브러리가 있다.

- `libc++_shared.so`
- `libgodot_android.so`
- `libgodotopenxrvendors.so`
- `libnightfall-stream.android.template_release.arm64.so`
- `libnightfall-xr.android.template_debug.arm64.so`
- `libopenxr_loader.so`

현재 APK의 파일명 중 `LICENSE`/`NOTICE`를 검색하면 Lucide 고지만 확인된다. 전체 GPL·MIT·Apache·NDK libc++ 및 정적으로 링크된 의존성 고지가 전달되었다고 판단할 근거가 부족하다. APK와 설치 ZIP에 라이선스 문서·대응 소스 안내를 함께 넣고, 앱의 About에서도 접근 가능하게 하는 공개용 작업이 필요하다.

최신 project의 `build/export-sources.json`은 최근 변경 GDScript·shader 8개의 해시 기록이다. `scripts/release/build_bundle.py --quest-source`가 요구하는 전체 `source-manifest.json`과 다르다. 기존 APK에 선택된 compiled script와 native library를 덧씌우는 delta 조립은 최근 변경이 제한되었다는 유용한 검증이지만, 새 사용자가 전체 APK를 소스에서 빌드할 수 있다는 증거는 아니다. upstream, Godot 엔진 패치, Nightfall native·Java·GDScript 변경, Moonlight 및 vcpkg 의존성, addon, Android/Gradle 입력과 각 고지를 포함한 최신 대응 소스 manifest를 생성해야 한다.

### Meta/OpenXR: 확인과 미확인

확인: export preset에서 Meta vendor plugin이 활성화되어 있고, addon에는 `meta/LICENSE-SDK`가 Meta SDK 약관을 가리키며 `LICENSE-LOADER`는 Apache-2.0이다. 공식 Vendors 본체는 MIT이나 vendor 구성요소의 별도 약관을 명시한다. 5.0 changelog는 loader가 Godot 쪽으로 이동했음을 기록하고, 실제 Meta AAR에는 vendor 라이브러리가 들어 있다.

추가 확인: `libopenxr_loader.so`는 위 Khronos Maven AAR의 arm64 native와 정확히 일치한다. OpenXR Vendors도 공식 `5.0.0-stable` ZIP의 native에 동일 strip을 적용하면 APK와 일치하며 tag의 MIT 고지를 확보했다. 미확인: `LICENSE-SDK`가 남아 있다는 사실만으로 현재 APK가 독점 Meta SDK 실행 코드를 포함한다고 단정할 수 없다. 반대로 vendor plugin 전체를 MIT라고 가정할 수도 없다. 실제 활성 vendor의 사용 헤더·소스·AAR·DEX와 proprietary SDK 바이너리 미포함 여부는 최종 빌드 구성 검토에서 확인해야 한다.

[공식 Meta SDK 약관](https://developers.meta.com/horizon/licenses/oculussdk/)은 1.2.8에서 SDK 자체에 오픈소스 의무가 적용되도록 배포하는 방식을 제한한다. 실제 독점 SDK와 GPL 코드가 결합된 것으로 확인되면 고지만 추가하는 방식으로 해결하지 않는다. Khronos Apache-2.0 loader와 공개 OpenXR 헤더를 사용하는 구성으로 분리·재빌드하고 실기 검증한다. 현재 감사는 위반 확정이나 기능 변경의 근거로 사용하지 않는다.

## NVIDIA·Qt·오디오 드라이버의 배포 경계

- NVIDIA: 현재 PC 패키지는 사용자 PC의 NVIDIA 드라이버를 이용하고 공식 Torch wheel을 설치한다. 드라이버 설치 파일·Toolkit 전체·별도 TensorRT 패키지를 무조건 복사하지 않는다. 향후 CUDA DLL을 직접 묶는다면 [정확한 CUDA 12.6 재배포 약관과 목록](https://docs.nvidia.com/cuda/archive/12.6.0/eula/index.html)에 따라 파일 단위로 확인하고 고지를 유지한다. 무료 사용 가능 여부와 오픈소스 여부를 구분한다.
- Qt: 현재 동적 wheel 설치 경로, 라이선스 텍스트, 원본 dist-info, 소스 버전 안내를 유지한다. 자체 변경 Qt나 폐쇄형 단일 실행 파일로 바꾸면 대응 소스·라이브러리 교체 조건을 다시 검토한다. `scripts/release/licenses/Qt-SOURCES.json`은 라이선스 파일 출처이고 Qt 전체 소스 archive manifest는 아니다.
- Steam Streaming Speakers: 현재 앱은 이미 설치된 정확한 활성 endpoint를 사용한다. 드라이버가 없으면 Quest only를 제한하며 기본 PC 출력과 PC + Quest 경로는 사용할 수 있다. 새 PC에도 Quest only가 자동 제공된다고 안내하지 않는다. 현 패키지에 Valve 드라이버를 넣거나 별도 재배포 권한이 확인되지 않은 자동 설치기를 추가하지 않는다. 공식 Sunshine에도 Steam이 설치된 경우에만 해당 드라이버 설치 경로가 존재한다.
- H.264/HEVC: Sunshine의 GPL 라이선스는 코덱 특허에 대한 별도 권리 부여를 의미하지 않는다. 무료 오픈소스 게시와 향후 상업 제공을 같은 판단으로 묶지 않는다. 본 공개 준비에서 코덱 특허 권리를 취득했다는 주장을 하지 않는다.

## 공개 APK 서명과 업데이트

현재 앱은 `app.questto3d.client.debug`와 개발용 서명을 사용한다. 공개 stable의 package ID·versionCode·release signing identity를 별도로 고정하고, release 개인키는 Git·ZIP·로그에서 제외한다. 공개 키 인증서의 SHA-256은 업데이트 확인용으로 기록할 수 있다. 개인키는 소스 공개 대상이 아니며 사용자가 자신의 키로 빌드하는 방법을 제공한다.

개발용에서 공개용 package ID 또는 서명이 달라지면 기존 앱의 무손실 덮어쓰기 업데이트가 되지 않을 수 있다. 현재 개발 앱을 자동 삭제하지 않는다. 공개 앱 병행 설치·설정 재연결 절차를 문서화하고, 첫 공개판에서 이후 공개판으로의 동일 서명 업데이트는 설정·페어링 보존을 실제 검증한다. 개발 APK를 기존 사용자에게 계속 제공하는 경로와 새 공개 release 경로를 분리한다.

## 새 소스 후보의 추가 확인 — 2026-10-01

새 API 호환 host의 native·web 빌드와 명령 실행은 통과했지만 공개 pin을 바꾸지 않았다. 실제 FFmpeg archive SHA `b293d7f6bd3f032ea01c7e4451b7db540622f2d603e8b98d336513895842c506`은 공식 [build-deps v2026.724.203728](https://github.com/LizardByte/build-deps/releases/tag/v2026.724.203728)의 digest와 일치한다. target commit `a9a9277cdafe8a0ff9f197915fe43b383ed4f36b`, 실제 libavcodec의 FFmpeg `38b8833`, 보존 upstream·patch·recipe도 연결됐다. 실제 GPL 빌드를 일반적인 LGPL FFmpeg로 고지하지 않는다. [FFmpeg 라이선스 안내](https://ffmpeg.org/legal.html)

MSYS2 실제 link의17개 package/19개 library는 고정 archive SHA 및 멤버 바이트와 일치했다. 새 exe에는 libiconv1.19-1·libidn2 2.3.8-4·libunistring1.4.2-1의 정적 symbol이 있다. 각 cached `.BUILDINFO`의 PKGBUILD SHA는 공식 MSYS2의 정확한 recipe commit과 일치했으나 upstream source·MSYS patch/recipe의 공개 공급 반영과 host/web 고지를 마감해야 한다. 이3개는 우선 수집 대상이며, GPL host에 실제로 포함되는 나머지 정적 component도 source/recipe 또는 구체적인 제외 근거를 전체 대응 소스 ledger에 연결한다. component 자체의 MIT/BSD 고지만으로 결합물의 source 공급이 완료됐다고 판단하지 않는다. 수집·다운로드와 공개 asset 포함은 구분한다. 이미 수정하지 않은 모든 toolchain/dependency를 다시 컴파일하거나 exe를 비트 단위로 맞추는 조건을 일괄 추가하지 않는다.

Quest는 source r3 489파일에서 새 Android engine·stream/XR을 컴파일하고 전체 project/Java/DEX를 export했다. unsigned public APK `a3e933791f9ee047619232ac56f0b350e1a0524151e13e8cd7437ede01bf54de`의 새 native는 NDK strip 후 SHA와 ELF build ID로 연결됐다. 실제 loader는 Khronos1.1.54이며 official vendor ZIP 입력도 추적했다. 이것이 dependency 고지·서명·실기 완료를 뜻하지 않는다. vendor release workflow는 Meta mobile SDK v77 preview headers와 `META_HEADERS_ENABLED`를 사용하므로 Meta SDK가 활성 빌드에 없다고 단정하지 않는다. 해당 헤더·upstream submodule·원 약관과 실제 포함 코드의 연결을 마감한다. 기존 개발 앱·키·APK를 교체하지 않았다.

[Host 빌드·입력·남은 범위](HOST_RELEASE_PREPARATION_2026-09-30.md) · [Quest 재빌드·활성 고지·남은 범위](QUEST_RELEASE_PREPARATION_2026-09-30.md)

## 공개 전 통과 조건

1. 최신 APK 해시에 연결된 전체 대응 소스 manifest와 pinned upstream·patch·build 입력을 제공
2. 깨끗한 디렉터리에서 host·Quest 빌드 절차 검증. 검사되지 않은 항목은 미검증으로 유지
3. 실제 APK의 외부 코드·정적 라이브러리·Android 의존성 목록과 고지 완성
4. Meta proprietary SDK 미포함 또는 적합한 분리 여부 확인. 파일 존재만으로 결론 내리지 않음
5. release 서명·package·version 정책 고정과 공개판 업데이트 보존 검증
6. 배포 ZIP의 모델·개인 자료·Steam/NVIDIA 드라이버·서명 개인키 제외 재검사

소스 코드 공개 준비와 사용자가 바로 설치할 수 있는 바이너리 릴리스의 준비 상태를 별도로 기록한다. 대응 소스가 불충분한 기존 APK를 다운로드 버튼으로 먼저 배포하지 않는다.
