# Quest 공개 앱 UI 보완 — 2026-10-01

공개 전 사용자 검토에서 확인한 뒤로가기·조절 아이콘 누락과 어색한 한국어 설명을 보완한다. 공개 package와 release 인증서는 유지하며 versionCode만 **1 → 2**로 올린다. 이전 APK·대응 소스·배포 묶음은 보존한다.

## 아이콘 누락 원인과 수정

이전 `ProductTheme.icon()`은 `FileAccess`로 SVG 원문을 읽어 매번 texture를 만들었다. 실제 code1 APK에는 precision 아이콘 SVG 원문이 없고 Godot의 `.svg.import`와 가져온 texture가 있다. 그래서 편집기에서는 보이지만 APK에서는 null이 반환되었다. Pretendard의 일반 방향 화살표 glyph 부재가 원인이라는 주장은 하지 않는다. 실제 OTF에는 해당 glyph가 있다.

새 코드는 18개 원래 Lucide 리소스를 `preload`로 가져와 export remap을 따른다. 방향 화살표는 원래 arrow-left의 회전으로 구성하고, 선택 색과 크기별 texture를 캐시한다. 탭의 `TextureRect`는 `MOUSE_FILTER_IGNORE`를 유지한다. 메뉴 뒤로가기·이동·±·닫기와 첫 화면의 Back이 같은 리소스 경로를 사용한다. 이는 누락과 반복 texture 생성 문제를 고친 것이며, 영상 선명도나 벡터 해상도를 개선했다고 주장하지 않는다.

Lucide 원래 ISC license와 Feather attribution을 `licenses/Lucide-ISC.txt`로 복사하고 Quest 배포의 실제 binary notice 목록에도 포함한다.

## 표시와 동작

- 주요 이름은 Display, Size, Position, Views, Environment, Framing, Depth, Distance, Quality, Connect와 기존 English 설정명을 유지한다
- 보조 설명은 짧고 자연스러운 한국어로 정리한다
- 거리 버튼은 25 cm, 슬라이더 중간 단계는 기존 10 cm
- `보이는 크기 유지 · Off`: 화면의 물리적 너비 유지, 멀수록 작게 보임
- `보이는 크기 유지 · On`: 거리와 너비를 함께 바꿔 겉보기 크기 유지
- 유효 거리 범위를 표시하고 끝점에 도달한 방향의 버튼은 비활성화
- 곡률과 크기 유지 옵션을 고려한 `view_distance_limits()`를 UI와 실제 setter가 공유
- step 배수가 아닌 유효 범위도 슬라이더 양 끝에서 정확히 선택

저장된 KeepSize 선택·설정 version1·preset 내부 이름·모델·Depth 기본값·송출 품질·전송 설정은 유지한다. 기본 KeepSize=false를 유지하며 기존에 저장한 true를 강제로 바꾸지 않는다.

## 검증 범위

`artifacts/quest/public-ui-refine-20261001`에 실패와 성공 로그를 함께 보존한다.

| 검사 | 결과와 의미 |
|---|---|
| editor 아이콘 리소스 | 86 checks 통과, cache 재사용·투명 배경·선택 색·방향 구분 |
| exported PCK 아이콘 | 86 checks 통과, **raw SVG absent** 상태에서 실제 remap 리소스 로딩 |
| 실제 Godot 메뉴 렌더 | Back·±·방향·Tilt/Roll 11개 버튼의 아이콘 표시를 제거 전후 비교, 각각 120–212 changed pixels |
| 탭 입력 | TabIcon non-null, mouse filter IGNORE |
| 원래 전체 메뉴 | Display/3D/Quality/Connect와 하위 메뉴·Welcome/Server/IP/PIN 이미지 생성 |
| 거리·수평·UI 회귀 | 최종 r4: Distance64 + Level222 + Display73 = **359 checks 통과**, 실제 setter와 RecordedRenderer API 전달 경계 |
| source delta helper | baseline·output 보존, SHA/inventory 변조·native source 침입 거부 7 tests |

최소 거리의 off-axis float 경계가 오래된 radius fallback을 발동하는 추가 문제도 독립 검토에서 확인했다. 유효 거리의 작은 반올림 오차가 큰 곡률 변경으로 이어지지 않도록 보완했다. 거리·수평·UI 전체 r4 회귀 **359 checks**가 통과했고 SCRIPT ERROR/REGRESSION FAIL은 없었다. 첫 fixture의 초기화 누락과 중간 테스트 실패는 성공으로 취급하지 않고 기록을 보존한다.

렌더 검증은 고정 Godot 4.7 Linux editor, Xvfb, 격리된 테스트 프로필에서 수행한다. 테스트에서는 실제 네트워크와 Android extension을 사용하지 않는다. Linux extension 미공급 경고는 기록하고, 실제 native OpenXR 표시나 착용 결과로 확대 해석하지 않는다. code2의 실제 Quest 업데이트·연결·화살표 착용 확인은 루트 통합 검증에서 별도로 기록한다.

## 재현과 공급

새 delta는 `patches/quest-public-ui-20261001`이며 기존 `quest-level-settings-20260930`을 변경하지 않는다. `scripts/prepare-quest-public-ui-build.py`는 exact baseline manifest와 각 파일의 before/after SHA를 검사한 뒤 새 output에 적용한다. 이미 존재하는 output을 덮어쓰지 않는다.

```powershell
python scripts/prepare-quest-public-ui-build.py --source <검증된 기존 pre-complete source> --output <새 source output>
```

아이콘의 실제 exported-resource 회귀는 WSL에서 실행한다.

```bash
bash <project>/test/validate_product_icons_export.sh <고정 Godot editor> <새 fixture output>
```

Android export에서는 `prepare_quest_android_export.py --version-code 2 --version-name 0.1.0-preview`와 `verify_quest_unsigned_apk.py --version-code 2`를 사용한다. 기존에 소스로 빌드하고 실제 APK에 대응시킨 own stream/XR·수정 Godot·public Khronos vendor·libc++·loader 입력을 재사용한다. Java/DEX와 GDScript를 새로 export하고 실제 APK의 native 6개 SHA를 이전 code1과 대조한다. 성능이나 native 바이너리를 새로 개선했다고 보고하지 않는다.

`complete_quest_source_supply.py`는 최종 APK의 실제 metadata와 대응 소스, exact dependency supply, overlay/recipe, Lucide 고지를 새 manifest에 묶는다. 최종 signed 대응 소스에는 수정된 전체 project가 있으므로 과거 delta를 다시 적용할 필요가 없다. 키와 비공개 설정은 공개 소스에 넣지 않는다.

## 최종 배포 검증 상태

배포 APK·대응 소스의 실제 SHA, 서명, 설치와 Quest 연결 검증 상태는 해당 GitHub Release의 `release-validation.json`과 `SHA256SUMS.txt`를 따른다. 작업 중 상세 증거는 STATUS와 artifacts의 JSON에 기록한다. UI 렌더·리소스·소스 회귀 성공을 실제 Quest 착용 검증이나 전체 완료로 취급하지 않는다.
