# Sunshine 호스트 공개 준비 검증

공개 준비 기록 · 2026-10-01 · 기존 제품 실행 파일 보존

## 현재 공개 후보 — 2026-10-01

**새 호스트 `2026.930.1`의 실제 대응 소스와 의존성 고지 공급을 마감했다**. 기존 개발 호스트 `77c950b5…`의 누락 원문이 복구된 것은 아니다. 새 `86eb2ee5…`는 명시적인 API 호환 수정으로 실제 빌드한 별도 baseline이며, 공개용 입력은 `artifacts/publication/host-release-inputs-20261001-final/HOST_RELEASE.json`이다. 아래 역사 조사·첫 후보 기록은 이전 실패와 판단 이유를 보존한다.

| 공개 준비 항목 | 실제 결과 |
|---|---|
| 새 exe | SHA-256 `86eb2ee5e3177a892f15ecd5ba869b27d3e1b9131848b1adacf9a25301767d42` · 67,192,649 B |
| 소스 공급 | `source-supply/sunshine-modified.tar.gz` · 15,603개 선택 source files · 115,848,414 B |
| 소스 archive SHA-256 | `741b811085c866d1b17a3631674691e88033d35f9b6a2ee1f31bc1e20e8de9ba` |
| 실제 source authority | original Git/submodule archive + 역사 overlay + 명시 API patch · 모든 archive member의 실제 SHA 비교 통과 |
| 의존성 source packages | 정확한 MSYS2 14개 · 105,773,101 B · 세 GNU package와 나머지 11개 |
| 나머지 11개 recipe | 실제 binary `.BUILDINFO` SHA와 공식 commit·source package recipe가 일치 · 37개 source/patch/signature 입력을 읽어 recipe checksum 대조 |
| 고지 | 실제 연결된 MSYS2 runtime 17개, web production npm 35개, vendored source와 Boost/JSON/NV header 원 copyright·license texts |
| 설치용 static runtime | payload 334개 + `runtime-preparation.json` · `kind=host-release-runtime` · 현재 제품 설정/credentials 미포함 |
| 실제 source/runtime notice 교차 검사 | 대응 source 파일과 runtime 파일 바이트 동일 · 전체 manifest mismatch 0 |
| archive의 알려진 개인 정보 검사 | 변경 소스/공급 metadata 및 원 upstream의 알려진 개인 홈·이메일·runtime marker 0 |
| 정상 upstream key fixtures | 원 공식 authority와 동일한 3개 source file로 분리 · 수정 입력/사용자 key 아님 |
| helper 경계 검사 | source 수집·release supply·public unpack·runtime staging 49 tests 통과 · 실패·skip 0 |
| native/web 기존 실제 검증 | native build + 35 CPU tests + web build 성공 |
| 실제 safe server 검증 | 아래 loopback 검사 두 회 성공 |
| `source_complete` / `notices_verified` / `native_build_verified` | `true` |
| Quest actual stream/wearer/audio/long-run 검증 | 이 새 호스트에서는 미검증 · source gate와 구분 |

원래 전체 조립 소스 중 **사용하지 않는 공식 파일 9,445개는 공급에서 제외**했다. AMF의 `Thirdparty/ffmpeg/`는 SDK 샘플용 FFmpeg source/prebuilt DLL·PDB 묶음이며 실제 build-deps `amf.cmake`는 `amf/public/include`만 복사한다. `file_to_header.exe`와 Vulkan Loader fixture data도 이번 실제 Sunshine dependency graph에 없다. 포함·제외 파일 SHA를 각각 `source-file-inventory.json`, `excluded-unused-upstream-inputs.json`에 보존했다. 실제 필요 AMF headers·FFmpeg 38b88335 source/patch·SVT/x264/x265·MIT-licensed NVAPI import libraries는 공급에 남긴다. 원 공식 archive의 불필요한 바이너리까지 재배포 범위를 넓히지 않는다.

`notices/`는 실제 선택 runtime에도 그대로 전달된다. `provenance.json.runtime_notice_files`가 source-supply와 runtime의 경로를 연결하고, `HOST_RELEASE.json`이 파일별 size/SHA를 고정한다. GCC runtime의 적용 파일에는 Runtime Library Exception 3.1을 구분하며, MinGW CRT/Windows POSIX threading은 일반 플랫폼 runtime의 원 고지를 보존한다. Windows OS DLL과 NVIDIA driver를 복사하지 않는다. Node/npm/GCC/CMake/Ninja 실행 도구 전체를 사용자의 설치 ZIP에 넣거나 그 전체를 재컴파일해야 한다는 조건은 추가하지 않는다. 수집한 MSYS2 분리 서명의 GPG 검증은 수행하지 않았으며 `SKIP` signature 입력을 checksum 검증으로 오인하지 않는다.

현재 제품의 monitor 확대 입력은 **frame bridge protocol 2**다. 이후 protocol-3/source-selection/file-PCM 실험은 이 호스트 baseline에 포함되지 않는다. 사용자가 제외한 inline 3D, PC pointer 입력, 내장 file player도 최종 공개 필수 기능으로 되돌리지 않는다. PC audio 기본값과 기존 system-loopback→Opus의 Quest/shared audio 경로는 유지해야 할 실제 제품 범위다.

### 실제 loopback 시작·종료·재시작

`artifacts/publication/host-loopback-probe-20261001-r2/loopback-validation.json`은 별도 runtime/config에서 수행한 실제 native 검사다. 원 sunshine process와 관련 listen ports가 없음을 확인한 뒤 `127.0.0.1`, base port `58189`, validation 이름으로 시작했다. audio/input/display 모드 변경과 UPnP는 껐고, 원 방화벽·운영 설정·오디오·페어링을 바꾸지 않았다.

- 실제 v2 BGRA publisher의 1280×360 합성 프레임을 새 native reader가 수신
- 실제 RTX 2060 SUPER에서 NVENC H.264·HEVC encoder 초기화 probe 성공
- HTTP `serverinfo` 200 준비 시간 4.39초/3.89초 · 두 번 시작
- 실행 중 동일 port의 두 번째 native copy를 exit 9로 거부
- 인증하지 않은 Quest control HTTPS 연결을 TLS 단계에서 거부
- graceful exit 0 두 번 · 임시 mDNS 등록·해제 두 번 · 최종 자신의 process/ports 소멸
- 원 개발 host SHA 불변

첫 검증의 두 번째 종료 timeout은 test harness가 부모 Python의 Windows Ctrl+C-ignore를 남겨 다음 child가 그 상태를 상속한 원인이었다. 원 실패를 `host-loopback-probe-20261001`에 보존하고, ignore 상태를 복구한 r2에서 같은 assertions로 재검증했다. 제품 코드를 바꾸거나 assertion을 낮추지 않았다.

이 검사는 **실제 native 실행·IPC·encoder probing·API·lifecycle**의 증거다. 합성 프레임에 대한 기술 검사이므로 실제 WGC capture→AI→stereo→Quest 수신 성능·입체 품질·wearer audio sync를 검증한 것으로 표시하지 않는다. release asset 최종 설치와 실제 Quest 연결의 결과는 제품 현황 기록에서 별도로 확정한다.

### 공개 source 재현 경로

source supply의 `SOURCE.md`, `tools/unpack_host_release.py`, `tools/rebuild_host_release.sh`를 제공한다. unpacker는 archive와 member의 실제 SHA 및 경로 경계를 먼저 검사하고 새 별도 build workspace만 만든다. build helper는 source·dependency inputs를 이 workspace에 연결하고 native test/web build를 실행한다. 설치·방화벽·서비스·서버 시작은 하지 않는다. 고정 Windows MSYS2 UCRT64 toolchain 및 native Node `v24.20.0`이 필요하며 `.BUILDINFO`-bound source recipes는 읽기 입력으로 공급한다.

```powershell
python source-supply/tools/unpack_host_release.py --supply source-supply --output host-rebuild
```

그 뒤 MSYS2 UCRT64에서 `QUEST3D_NATIVE_NPM`에 고정 native Windows `npm.cmd`를 지정하고 `bash source-supply/tools/rebuild_host_release.sh host-rebuild 6`을 실행한다. dependency archives와 `toolchain.lock.json`의 download URLs·SHA를 유지한다. 기존 바이너리와 bit-for-bit 일치를 공개 조건으로 추가하지 않는다.

최종 root 공급 JSON SHA-256은 `49584877ab43fabc81a70bd8bc5f10d6df0e8f85c81fa9bd078bbd87615a8fcb`다. `source-runtime-binding-and-privacy-audit.json`과 실제 loopback 원 보고서는 공개 후 자동으로 새 실기 검증을 통과시키는 자료가 아니다.

같은 공개 unpacker를 최종 source-supply 실제 파일에 실행했다. `artifacts/publication/host-public-unpack-validation-20261001`의 새 workspace에 15,603 source members의 실제 SHA와 전체 dependency archive SHA를 확인해 추출했으며 exit 0이다. 실제 원 source/dependency/제품을 덮어쓰거나 build/server를 실행하지 않았다. 이 결과는 source 준비 도구의 작은 fixture tests와 별도로 실제 공개 입력의 unpack 경로를 확인한다.

그 새 추출 tree로 실제 CMake configure도 exit 0, configure 64.9초·generate 2.9초로 완료했다. Ninja dry-run은 exit 0이며 glob check/CMake regeneration을 계획했다. 여기서 native compile을 다시 실행한 결과로 표시하지 않는다. 고정 실제 source 입력의 기존 성공 빌드와 새 공개 공급의 unpack/configure를 구분하며, `host-public-unpack-validation-20261001/PUBLIC_SUMMARY.json`에 기록했다.

## 결과

**현재 보존한 역사적 소스 스냅샷은 빌드에 필요한 변경 파일이 누락된 상태다**

공식 upstream commit과 역사적 overlay를 새 폴더에 조립한 결과 25,047개 파일이 archive authority와 정확히 일치했다. 그러나 실제 native build는 `src/stream.cpp:1879`의 audio packet 접근에서 실패했다. 소스를 정확히 복사했다는 검증과 배포 바이너리에 대응하는 완전한 소스라는 검증은 별개다. 이 자료를 완전한 corresponding source 또는 설치 가능한 release로 표시할 수 없다.

| 항목 | 확인 결과 |
|---|---|
| 기존 사용 중 호스트 SHA-256 | `77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63` |
| Sunshine upstream commit | `cb72dffa3233c5815cd5ba88f09f049dd679ba75` |
| 역사 overlay | `artifacts/host/patch-reproduction-f6e4f3c7490a4c9eac9cf2ea37a4342d` · 49개 파일 · 2026-09-09 22:53 저장 |
| 당시 회귀 자료 | `artifacts/host/host-regression-20260909-225104.xml` · 75 tests, 0 failures |
| 이번 isolated source | `artifacts/publication/host-source-prep-20260930/source` |
| 조립 소스 권위 비교 | 25,047개 파일 · missing/extra/changed 모두 0 |
| 포함된 initialized submodules | Windows 관련 및 nested 34개 |
| Configure | 성공 · 약 54.3초 |
| Native build | 실패 · 6 jobs · 새로운 호스트 exe 미생성 |
| 기존 실행 파일 교체·서버 실행 | 없음 |
| 역사 바이너리와 소스의 대응 검증 | 미완료 |

## 실패 원인

역사 overlay의 `src/audio.h`는 `audio::packet_t`를 `first`, `second`, `file_scope` 필드를 가진 struct로 변경한다. 하지만 `src/stream.cpp`는 overlay에 없다. 따라서 공식 upstream의 다음 tuple 접근이 남는다.

```cpp
TUPLE_2D_REF(channel_data, packet_data, *packet);
```

이 매크로는 `std::get<0>`과 `std::get<1>`을 호출하므로 struct와 호환되지 않는다. 실제 GCC 오류는 `no matching function for call to get<0>(audio::packet_t&)`였다. 나중의 현재 작업 소스에는 packet 필드 접근과 추가 lifecycle 변경이 존재하지만, 그 파일을 과거 바이너리의 원본이라고 간주할 근거가 없다. 원래 역사 source 검증에는 추정 patch를 적용하지 않았다. 이후 별도 격리한 최소 호환 후보는 아래에 구분해 기록한다.

`src/stream.cpp`, `src/stream.h`, `src/video.cpp`의 당시 변경 유무와 전체 build inputs를 재확인해야 한다. 이 중 이번 빌드로 직접 확정한 누락은 `src/stream.cpp`다. 다른 파일의 누락을 이번 오류만으로 단정하지 않는다.

## 입력과 격리

- GCC `16.2.0-3`, CMake `4.4.3-2`, Ninja `1.13.2-1`, MSYS2 UCRT64
- 기존 A 드라이브 설치 도구 사용 · 네트워크 다운로드 없음
- Boost `1.89.0` 원본 archive SHA-256 `67acec02d0d118b5de9eb441f5fb707b3a1cdd884be00ca24b9a73c995511f74`
- nlohmann JSON `3.11.3` 원본 archive · upstream MD5 pin 및 추가 SHA-256 기록
- NVENC SDK 11/12/13 header의 로컬 Git object archive와 commit 기록
- FFmpeg prebuilt archive `Windows-AMD64-ffmpeg.tar.gz` · build-deps `v2026.724.203728` · SHA-256 기록
- 현재 dirty Sunshine checkout에서 파일을 복사하거나 reset하지 않고, selected Git commit archive와 별도 역사 overlay만 사용
- build output, dependency unpacking, npm cache, 임시 폴더는 새 A 드라이브 준비 경로
- archive path traversal, Windows reserved path/case alias, link·special-file 거부 테스트 9개 통과

FFmpeg 정적 라이브러리와 해당 build-deps 소스·옵션의 대응, 기타 배포 의존성의 소스·라이선스 충족은 독립된 공개 전 확인 항목이다. 이번 configure 성공만으로 해결된 것으로 표시하지 않는다.

## 백업 조사와 PE 비교

읽을 수 있는 `artifacts/host`의 여러 patch-reproduction 폴더, `artifacts/audio`, `.cache`의 관련 파일을 조사했다. 이후 시점의 `stream.h` 일부는 있지만 당시 `stream.cpp` 또는 정확한 전체 변경 patch는 발견하지 못했다. 접근 거부된 테스트 폴더는 우회하지 않았다. 따라서 “확인한 백업에서 미발견”이며 영구 복구 불가능하다는 결론은 아니다.

당시 audio coverage 자료에는 `.gcda` 실행 카운터가 있으며 정확한 C++ 원문 백업으로 사용할 수 없다. 실행 파일의 문자열이나 역어셈블리만으로 원문을 추정하여 공식 corresponding source로 표시하지 않는다.

기존 pinned exe와 이미 존재하던 후대 `cmake-build-quest3d/sunshine.exe`를 실행 없이 PE 섹션으로 비교했다. 전체 파일은 다르고 21개 섹션 중 4개만 같았다. 이 비교는 **역사 overlay의 성공한 재빌드 결과가 아니다**. 후대 exe가 같은 원본이라고 볼 근거가 되지 않으며, 비트 단위 일치를 공개의 필수 요건으로 정한 것도 아니다.

## 안전한 다음 기준

1. 당시 누락 입력을 신뢰할 수 있는 백업·정확한 변경 기록에서 찾으면, commit + 전체 patch + submodule + dependency inputs를 고정하고 새 폴더에서 다시 빌드
2. 복구할 수 없다면 현재 검토 가능한 전체 소스를 별도 release baseline으로 확정하고 새 호스트 candidate 생성 · 기존 pinned 제품은 유지
3. API 호환만 추가한 추정 복원은 별도 실험 candidate로 표시 · patch provenance와 source correspondence 미검증 상태 유지
4. 새 baseline의 PC 실행·종료·재실행, discovery/pairing, 첫 연결·재연결, HEVC, 2D/3D 전환, 해상도·화면비, 설정 보존, 음성 세 모드·route 복구를 검증
5. Quest 2/3 실제 영상·입체·음성 확인과 장시간 동작 검증 후 release asset 후보로 승격

과거 바이너리의 bit-for-bit 재현 여부와 완전한 소스·기능 재현 가능성은 구분한다. 실제 배포할 바이너리의 정확한 소스 및 build inputs가 검토 가능하고 재빌드·동작 검증을 통과하는 것이 목표다.

## 재현 명령

개발 준비 환경에 이미 고정한 Sunshine checkout, 역사 overlay, MSYS2 도구와 dependency archive가 있을 때만 실행한다. 일반 사용자 설치 명령이 아니다. 기존 출력 경로를 덮어쓰지 않는다.

```powershell
.venv\Scripts\python.exe scripts/release/prepare_host_source.py --output artifacts/publication/host-source-prep-new
$env:QUEST3D_NATIVE_NPM = (Join-Path $PWD 'native/host/tools/node-v24.20.0-win-x64/npm.cmd').Replace('\','/')
$taskBuild = (Join-Path $PWD 'artifacts/publication/host-source-prep-new/rebuild_host_source.sh').Replace('\','/')
& native/host/tools/msys64/msys2_shell.cmd -defterm -here -no-start -ucrt64 -c "bash '$taskBuild' 6"
```

현재 확인된 역사 source 결함이 복구되기 전에는 위 build도 같은 이유로 실패하는 것이 예상 결과다.

준비 도구:

- `scripts/release/prepare_host_source.py` · archive authority 기반 소스 조립·해시·reparse 감사
- `scripts/release/rebuild_host_source.sh` · offline isolated configure/build, 호스트 미실행
- `scripts/release/compare_host_images.py` · 실행 없는 PE 섹션 비교, 대응 검증 상태 자동 승격 없음
- `tests/test_host_source_preparation.py` · source archive 경계 회귀 테스트

로컬 증거:

- `source-preparation.json` · source·overlay·submodule·dependency identity
- `source-authority-verification.json` · archive authority와 조립 파일 비교
- `build-failure-summary.json` · 공개 가능한 실패 요약
- `historical-vs-later-image-comparison.json` · 기존 exe 두 개의 PE 비교
- `msys2-packages.actual.txt`, `compiler.version.txt`, `cmake.version.txt` · 실제 도구 버전
- `build-private.log` · 절대 경로가 포함된 로컬 상세 로그, 공개 asset에서 제외

모든 로컬 증거의 기본 경로는 `artifacts/publication/host-source-prep-20260930`다.

## 별도 최소 API 호환 후보

실제 native 빌드·CPU 테스트 통과한 대체 후보 · 역사 바이너리의 원 대응 소스 복원 아님 · release source gate 미통과

`artifacts/publication/host-source-compat-candidate-20260930`에 archive와 49개 overlay로 25,047개 base 파일을 독립 조립했다. base authority 비교를 통과한 후 `src/stream.cpp`의 tuple 매크로 한 곳만 `packet->first`, `packet->second` 참조로 바꿨다. 현재 checkout의 후대 `stream.cpp`를 복사하지 않았다. 원래 CRLF를 그대로 유지하며 before/after SHA-256과 unified patch를 남겼다.

제품 변경은 다음 한 곳이다.

```cpp
auto &channel_data = packet->first;  ///< Session channel carried by the audio packet.
auto &packet_data = packet->second;  ///< Encoded Opus payload carried by the audio packet.
```

추가한 `tests/unit/test_quest3d_audio_packet_compat.cpp`는 실제 queue를 통과하는 정상 packet의 channel·payload 주소·바이트와 file packet의 scope identity 보존을 검사한다. 실제 네트워크 전송, file flush barrier, FEC, 영상·음성 실기 동작을 입증하는 테스트는 아니다. 특히 이 최소 patch는 누락된 역사 file-scope send/flush 동작을 복원하지 않는다.

| 항목 | 상태 |
|---|---|
| 후보 버전 | `2026.930.1` |
| 후보 branch | `quest3d-api-compat-candidate` |
| 후보 COMMIT label | `cb72dffa3233c5815cd5ba88f09f049dd679ba75-api-compat-candidate` |
| 제품 변경 파일 | `src/stream.cpp` 한 곳 |
| 추가 테스트 파일 | `tests/unit/test_quest3d_audio_packet_compat.cpp` |
| patch 재적용 경계 검사 | `git apply --reverse --check` 통과 |
| Python source/patch 안전성 테스트 | 19개 통과 |
| 별도 가벼운 native packet probe | 실제 test object + 기존 gtest/common support · 2개 tests 통과 · 마지막 실행 1 ms |
| configure/build/native unit tests | 성공 · native build exit 0 · 35 tests/6 suites, 실패·skip 0 · 54 ms |
| 전체 configure/build/test 소요 | 2,092초 · 약 34분 52초 · 6 jobs |
| 후보 sunshine.exe | 67,192,649 bytes · SHA-256 `86eb2ee5e3177a892f15ecd5ba869b27d3e1b9131848b1adacf9a25301767d42` |
| 기존 runtime·pin 변경 / 서버 실행 | 없음 |
| 역사 바이너리 source correspondence / release source gate / hardware validation | 모두 미통과 |

source와 `cmake-build-compat-candidate`, 임시 폴더, npm cache는 후보 전용이다. 기존 준비 폴더의 unpacked Boost·JSON·NV header·FFmpeg는 읽기 dependency 입력으로만 공유한다. CMake FetchContent/CPM은 disconnected이고 npm도 offline이며, 이번 대상은 native `sunshine`과 `test_sunshine`이다. web-ui 패키징, 설치 실행과 실제 송출은 이번 native compile 범위에 포함되지 않는다.

새 도구:

- `scripts/release/prepare_host_compat_candidate.py` · independent assembly, exact-site patch, provenance, 기존 출력 덮어쓰기 거부
- `scripts/release/rebuild_host_compat_candidate.sh` · candidate 전용 버전, offline configure/native build/CPU unit tests, 단계·exit code 요약
- `tests/test_host_compat_candidate.py` · 한 곳 변경, CRLF 보존, duplicate/missing site 거부, 원본/기존 후보 보존

기존 역사 준비 폴더가 있는 개발 환경에서 새 후보를 준비하는 명령:

```powershell
.venv\Scripts\python.exe scripts/release/prepare_host_compat_candidate.py --historical artifacts/publication/host-source-prep-20260930 --output artifacts/publication/host-source-compat-new
$env:QUEST3D_COMPAT_DEPS = (Join-Path $PWD 'artifacts/publication/host-source-prep-20260930/dependencies').Replace('\','/')
$env:QUEST3D_NATIVE_NPM = (Join-Path $PWD 'native/host/tools/node-v24.20.0-win-x64/npm.cmd').Replace('\','/')
$taskCompatBuild = (Join-Path $PWD 'artifacts/publication/host-source-compat-new/rebuild_host_compat_candidate.sh').Replace('\','/')
& native/host/tools/msys64/msys2_shell.cmd -defterm -here -no-start -ucrt64 -c "bash '$taskCompatBuild' 6"
```

이 명령은 내부 source-gap 조사 재현용이다. 역사 overlay·archive가 포함되지 않은 공개 repository만으로 실행되는 일반 사용자 빌드 명령으로 표시하지 않는다. 중단된 source-only 폴더의 `--finalize-only`도 전체 base authority 재확인 후에만 patch하며, 이전 patch/manifest/output이 있으면 거부한다.

실제 후보 증거:

- `candidate-provenance.json` · base archive identity, patch, source SHA, 후보 버전, 미통과 gate
- `audio-packet-api-compat.patch` · 제품 변경과 추가 테스트 원문
- `candidate-build-summary.json` · actual exit code·phase·elapsed time·exe SHA·tests 상태
- `candidate-regression.xml` · native 테스트 실행 시 결과
- `packet-compat-probe.xml`, `packet-compat-probe-summary.json` · 신규 테스트 2개 별도 실행, source/object/library SHA 기록
- `build-private.log` · 상세 로컬 로그, 공개 asset에서 제외

이 후보의 native 빌드는 통과했으며 추가 API 누락으로 인한 compiler/linker 오류는 없었다. 하지만 기존 바이너리의 원 대응 소스와 완전한 공개 release로 자동 승격하지 않는다. 기존 runtime에 적용하거나 새 호스트를 실행하지 않았고, web-ui 패키징·설치·영상 송출·음성 라우팅·Quest 실제 검증은 남아 있다.

가벼운 packet probe는 같은 빌드의 test object를 별도 GoogleTest main 및 이미 빌드된 test-support/common library에 링크했다. 처음 support library를 생략한 링크는 해당 harness symbol 부족으로 실패했고, 기존 library 입력을 명시한 재시도에서 통과했다. 제품 소스를 추가 수정하거나 failing assertion을 낮추지 않았다. 전체 `test_sunshine` 실행과 다른 검증으로 구분한다.

native Windows GoogleTest는 MSYS의 `xml:/a/...` 절대 경로를 `A:/a/...`로 해석했다. 첫 report와 이번 실행에서 새로 생성한 공통 test log를 후보 증거 폴더로 보존해 모았으며 기존 파일은 덮어쓰지 않았다. 공개 helper는 candidate cwd와 상대 XML 파일명으로 보완했다. 실제 빌드에 사용한 helper 원문·SHA는 후보 안에 보존한다. 이 경로 문제를 고친 packet probe 재실행에서도 2개 assertion tests가 그대로 통과했다.

후보 실행 종료 후 원래 역사 `src/stream.cpp`, overlay 49개와 pinned runtime SHA가 그대로인 것을 재확인했다. 지금 확보한 것은 **명시적 작은 API 변경으로 빌드 가능한 새 후보**다. 다음 단계는 full source baseline을 검토해 실제 새 release binary와 source를 함께 고정하고, 앞서 적은 PC·Quest 동작을 검증하는 것이다.

## 새 후보의 web 빌드와 runtime 준비 — 2026-10-01

같은 API 호환 후보에서 고정 `package-lock.json`의 npm 의존성을 후보 전용 A 캐시로 준비했다. `npm ci --ignore-scripts --no-audit --no-fund`는 exit 0, CMake `web-ui` target도 exit 0이다. 고정 native Windows Node `v24.20.0`을 사용했으며 npm 설치 스크립트를 실행하지 않았다. Codecov는 local repository의 dry-run 경로와 토큰 없는 process 환경으로 실행했다. 라이브러리를 최신으로 갱신하거나 원래 host·source·설치 설정을 복사하지 않았다. Vite가 기록한 빌드 시간은 7.66초이며 dependency 다운로드 시간을 포함하지 않는다.

`scripts/release/prepare_host_runtime_candidate.py`로 새 `artifacts/publication/host-runtime-compat-candidate-20260930`에 125개 static 파일을 준비했다. 실제 새 exe·명시적인 build toolchain의 zlib·GPL LICENSE·source의 common/Windows assets·새 web build만 포함한다. CMake의 shaders junction은 따라가지 않고 검증한 source의 실제 shader 파일을 읽었다. runtime/state/config/credentials·서비스 설치·방화벽 스크립트를 원래 runtime에서 가져오지 않는다. 11개 경계 검사는 기존 출력/겹치는 경로, 미완성 web, 실패한 native 보고서, 변경 exe와 중복 assets를 거부하며 통과했다.

새 runtime의 `sunshine.exe --help`는 Windows 기본 라이브러리 환경에서 exit 0, 약0.541초로 완료됐다. 도움말은 config 생성·서버 초기화 전에 반환하는 코드 경로다. **이것은 서버 시작·실제 NVENC·Quest 송출 검사와 다르다.** 현재 새 서버를 시작하지 않았으며 PC 기존 설정·runtime pin·페어링·오디오·방화벽·Quest 설치는 그대로다.

추가 증거:

- compile 후보 `web-build-summary.json`, `web-build-private.log`, `web-dependencies-private.log`
- runtime 후보 `runtime-preparation.json`, `cli-probe.json`, `help-private.txt`
- `tests/test_host_runtime_candidate.py` · 11개 통과

재현 시 위 native candidate build 완료 후 같은 고정 Node/npm 및 후보 A 캐시로 `web-ui`를 빌드한다. static runtime 준비:

```powershell
.venv\Scripts\python.exe -B scripts/release/prepare_host_runtime_candidate.py --candidate artifacts/publication/host-source-compat-candidate-20260930 --zlib native/host/tools/msys64/ucrt64/bin/zlib1.dll --output artifacts/publication/host-runtime-new
```

입력 zlib SHA는 기록되며 모든 입력 dependency의 소스 재빌드·활성 고지 완료를 자동으로 의미하지 않는다. 이 단계로 web 패키징 누락은 해소했지만 역사 바이너리의 원 대응 소스, 누락된 file-scope 동작, 새 후보 실제 streaming·오디오·설치·마이그레이션, 전체 대응 소스/고지, Quest2/3 실기 gate는 미통과다. 기존 d ZIP/Git 후보는 변경하지 않았다.
