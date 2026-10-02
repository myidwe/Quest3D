# 소개 영상 · Video overview

[가로 영상 · Landscape](../../README.md#소개-영상) · [세로 영상 · Vertical](VERTICAL.md)

PC의 2D 화면 → 앱 실행·Quest 연결 → 깊이 추정·좌우 눈용 영상 생성 → Quest 입체 감상 → 화면 조절 순서의 24초 소개 영상입니다. 한국어·영어 README에서 가로 영상을 바로 재생할 수 있습니다.

## 화면 자료

- **앱 UI**: 실제 Qt/Godot UI를 샘플 설정으로 PC에서 렌더한 화면
- **7–11.5초 처리 예시**: 생성한 자동차 사진을 Sterevi의 실제 로컬 모델·합성 코드로 처리한 정지 프레임의 깊이와 좌우 영상
- **모니터·자동차·헤드셋 광고 장면**: built-in ImageGen으로 제작한 사용 흐름·입체 감상 연출

자동차의 평면·돌출 광고 이미지는 실제 모델의 변환 전후 비교가 아닙니다. 헤드셋에서 녹화한 영상도 아닙니다. 실제 처리 예시는 Depth Anything V2 Small·FP16·AI 504×280·눈별 1280×720·Depth 1.31%·comfort·forward-cuda를 사용했습니다. 입력·모델·출력 해시와 설정은 [처리 기록](PROCESSING_EXAMPLE.json)에 있습니다.

## 자막과 제작 사양

[한국어 SRT](captions.ko.srt) · [English SRT](captions.en.srt)

영상에는 한국어 문구와 직접 합성한 음악이 포함되어 있으며 내레이션은 없습니다. SRT는 별도 플레이어용 파일로, GitHub 플레이어에 자동 적용되지는 않습니다. 가로 1920×1080·세로 1080×1920, 24초·60fps·H.264/AAC입니다. 영상 제작 사양이며 앱의 처리 FPS나 헤드셋 표시율을 보장하지 않습니다.

소개 영상 코드·UI·음원은 프로젝트의 [GPL-3.0](../../LICENSE)와 [구성 요소 고지](../../THIRD_PARTY_NOTICES.md)를 따릅니다. Pretendard 폰트는 SIL OFL 1.1, Depth Anything V2 Small 가중치는 고정 모델 기록의 Apache-2.0 조건을 따릅니다. AI 이미지의 독점 권리를 주장하지 않습니다.

## English

The 24-second overview shows flat PC viewing, Sterevi setup, local depth estimation and left/right image synthesis, illustrative Quest stereo viewing, and screen controls. The app UI is a real PC-rendered sample state. The processing cards are actual offline outputs from a generated car input; the flat/pop-out advertising images are separate illustrations, not a measured conversion comparison. This is a regular 2D video, not headset footage. The 60fps film specification does not promise real-time app performance. English subtitles are supplied as a separate SRT for external players.

## 파일 무결성 · File integrity

| 영상 | 크기 | SHA-256 |
|---|---:|---|
| 가로 · Landscape | 4200038 bytes | `bb0c956f11a34f1edac654b3cf2012980cab2d60069f441a3c979a6e65c4b7e9` |
| 세로 · Vertical | 4591003 bytes | `8a9764672c3e43c284dff7b60f9dd0f9ee87dd18260cc483919567468380aa81` |

게시: 2026-10-03 · R9. 위 값은 전체 디코딩·색 정보·음원 검사를 통과한 최종 MP4의 크기와 SHA-256입니다.
