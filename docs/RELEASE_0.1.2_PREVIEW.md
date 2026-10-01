# 0.1.2-preview · EXE 설치

Windows와 Quest USB 설치를 EXE에서 시작하는 배포다. ZIP 압축 해제·CMD 실행을 기본 사용 흐름에서 제거한다. 설치 위치 선택·다운로드·설정 보존 업데이트·복구·방화벽 승인·Quest USB 설치는 기존 검증 로직을 재사용한다.

## 다운로드

- Windows: `Quest3D-Desktop-Setup-0.1.2-preview.exe`
- Quest USB 설치: `Quest3D-Quest-Setup-0.1.2-preview.exe`
- 고급·수동 설치: 같은 버전 Desktop/Quest ZIP
- 전체 대응 소스: `Quest3D-Source-0.1.2-preview.zip`
- 파일 해시와 실제 검증 범위: 해당 Release의 checksum·privacy·setup 검증 보고서

기존 0.1.1-preview 파일과 태그는 보존한다. 새 EXE/ZIP에는 개인 설정·모델·페어링·서명 개인키가 없다. 공식 원본의 저작자 고지와 시험용 자료는 출처 확인 후 유지한다.

## 버전 구분

PC 앱과 설치 묶음은 0.1.2다. **Quest APK는 0.1.1-preview/code3 그대로**이며 SHA256은 `a251af67634cf07d83cafdbda5eeeee38ab3dc02eee6a8a3cda4fb077515df6d`다. 동일 공개 package·인증서이며 native·모델·영상·음성·헤드셋 기능 변경은 없다. 기존 APK를 다시 설치할 필요는 없다. 이전 실기 결과를 새 실기 검사로 보고하지 않는다.

## 사용

1. PC Setup EXE 실행 → 설치 폴더 → 설치 또는 업데이트
2. 필요한 경우 설치창의 **연결 허용**, Windows 승인 → 설치창 닫기
3. Quest Setup EXE 실행 → USB 기기 확인 → 설치
4. **Quest3D Desktop → PC 시작**, Quest의 기존 PC **Connect**

처음 PC 설치에는 인터넷·고정 Python·GPU 라이브러리·모델 다운로드가 필요하다. 이후 AI는 로컬에서 실행한다. 지원 GPU는 기존 NVIDIA Turing(sm75) 범위를 유지한다. Windows 신뢰 서명과 다른 PC의 새 설치·전체 실기 승인은 별도 미검증이다.

[설치 안내](DISTRIBUTION.md) · [EXE 구조·빌드·검증 기준](EXE_INSTALLERS.md) · [이전 개인정보 수정](PRIVACY_REMEDIATION_2026-10-01.md)
