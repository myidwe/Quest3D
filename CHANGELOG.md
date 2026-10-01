# Changelog

## Preview UI corrections — 2026-10-01

- Exported Quest icons use imported resources, including Back and settings icons
- Distance shows actual curved-screen limits and distinguishes apparent-size preservation
- Natural Korean hints while retaining familiar setting names
- Public APK versionCode 2 under the same release signing identity

## 0.1.0-preview — 2026-10-01

- Windows 모니터 캡처 → 로컬 깊이 AI → 좌우 합성 → Quest 표시
- Quest 2·Quest 3 출력 프로필, HEVC, 2D/3D와 Depth 미세 조절
- DAv2 Small / DAD Small, 윤곽 안정화, Standard / Quality · Preview
- Windows 데스크톱 UI·트레이·PIN 연결·진단
- Quest 화면·메뉴 설정, 기본 수평 정렬·별도 Free 정렬
- PC 기본 소리, 선택형 Quest only / PC + Quest
- Windows 설치창, 호환 버전 업데이트·중단 복구·이전 버전 복원·설정 보존
- Quest USB 기기 선택·승인 상태 안내·데이터 보존 설치·downgrade 차단
- 한국어·영어 안내, 별도 Git 공개 후보·실제 Git tree·배포 ZIP 무결성 검사

- 새 host native와 공개 Quest package의 실제 소스 재빌드·대응 source/notice 공급
- 공개 Khronos header vendor, AAR native 선택 대조, 장기 공개 APK 서명
- 실제 새 PC 설치·캡처/AI/제어/종료/재시작, 호환 업데이트 데이터 보존
- Windows 방화벽 Description 제약 수정과 exact 앱·Private/LocalSubnet 실제 적용 확인

지원 장비와 미검증 조건을 제한한 첫 Preview입니다. [배포 안내](docs/RELEASE_0.1.0_PREVIEW.md)와 같은 Release의 `release-validation.json`을 확인합니다.
