# Quest3D 0.1.0-preview — 릴리스 초안

**게시 전 초안. 아래 확인 항목이 채워지기 전 GitHub Release를 게시하지 않는다.**

Windows 화면을 Quest 2·Quest 3에서 2D 또는 로컬 AI 스테레오 3D로 보는 첫 Preview.

## Download

1. Windows: `Quest3D-Desktop-0.1.0-preview.zip`
2. Quest: `Quest3D-Quest-0.1.0-preview.zip`
3. 대응 소스: `Quest3D-Source-0.1.0-preview.zip`
4. 무결성: `SHA256SUMS.txt`

실제 파일 링크·크기·해시는 게시할 후보에서 작성한다. GitHub 자동 Source code ZIP은 native 대응 소스 묶음을 대신하지 않는다.

## Start

PC ZIP 전체 해제 → Install-Quest3D.cmd → Quest 개발자 모드/USB 승인 → Install-Quest.cmd → 같은 사설망 → PC 시작 → Quest Scan/Pair → PC PIN 승인 → Connect.

## Compatibility

Windows x64 + NVIDIA Turing sm75 전용. 실측 RTX 2060 SUPER 8GB. Quest 2·Quest 3, 16:9. Quest 3의 Full SBS4096×1152는 HEVC 사용. 다른 GPU 세대와 모든 Turing 모델을 검증한 릴리스가 아니다.

PC 소리가 기본. PC + Quest는 동시 출력, Quest only는 별도 활성 Steam Streaming Speakers 필요. 이 드라이버는 동봉하지 않는다.

## Validation — 게시 시 실제 결과로 작성

- [ ] Git tag / commit / 공개 APK package·version·서명 공개 지문
- [ ] PC/Quest/Source ZIP 이름·크기·SHA256와 대응 관계
- [ ] native 소스·라이선스 목록·재빌드 결과
- [ ] 새 Windows 사용자/PC의 설치·Scan·Pair·영상·종료
- [ ] Quest 2·Quest 3의 최종 APK 착용 결과와 미검증 범위
- [ ] 오디오 모드·복원·청취·AV 오차
- [ ] 업데이트·설정 보존·복구·제거
- [ ] 장시간 FPS·지연·VRAM·drop 기록

## Known limits

얇은 경계·가려진 배경의 윤곽 오차, 장면별 품질 차이, DRM·캡처 차단, 16:9 및 GPU 지원 제한. 기능별 미검증 항목은 숨기지 않는다. 새 AI 영상60FPS 또는 모든 장비30FPS 이상을 약속하지 않는다.

기능·성능은 해당 릴리스 결과를 기준으로 작성한다. 개발 중 실험이나 이전 ZIP 결과를 이번 릴리스 통과 기록으로 옮기지 않는다.
