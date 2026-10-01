# EXE 설치본

## 사용자 흐름

Windows 사용자는 **Quest3D-Desktop-Setup-0.1.2-preview.exe**를 다운로드하고 실행한다. ZIP 압축 해제나 CMD 실행 없이 설치창이 열린다. 설치 폴더를 선택한 뒤 **설치**를 누른다. 기존 설치 폴더라면 **업데이트**를 사용한다. 설치와 연결 설정을 마친 뒤 설치창을 닫는다. 이후 **Quest3D Desktop** 바로가기로 실행한다.

Quest는 **Quest3D-Quest-Setup-0.1.2-preview.exe**를 실행한다. 기존 USB 설치창에서 공식 ADB 경로와 연결된 기기를 확인하고 **설치**를 누른다. Quest 개발자 모드·USB 디버깅 승인은 헤드셋에서 직접 진행한다. Quest APK는 검증된 0.1.1-preview/code3과 동일하며 새 APK나 새 Pair가 필요하다는 뜻이 아니다.

처음 PC 설치의 고정 Python·GPU 라이브러리·모델 다운로드는 여전히 필요하다. 라이브러리 다운로드는 수 GB이며 모델은 약 99 MB다. EXE에 모델·사용자 설정·개인 키·페어링을 넣지 않는다. 첫 모델 설치 이후 AI 처리는 로컬에서 실행한다.

## 권한과 Windows 조건

EXE는 `asInvoker`로 실행한다. 관리자 권한을 자동 요청하거나 Windows 정책을 바꾸지 않는다. 설치 후 **연결 허용** 버튼이 기존 앱 소유 사설망 규칙에 필요한 UAC 승인을 요청한다. 승인 취소 시 방화벽을 변경하지 않는다.

Windows x64와 .NET Framework 4.8 이상을 대상으로 한다. Windows 11과 Windows 10 1903 이후에는 해당 런타임이 포함된다. 별도 .NET SDK나 컴파일러는 설치 사용자에게 필요하지 않다. Windows 10의 지원 수명과 다른 PC 검증은 설치 파일 형식과 별개의 조건이다. [Microsoft 공식 런타임 안내](https://learn.microsoft.com/en-us/dotnet/framework/install/on-windows-and-server)

Windows 신뢰 코드 서명은 아직 없다. SmartScreen 또는 관리 PC 정책에서 경고·차단이 생길 수 있다. 공개 APK의 Android 서명과 Windows EXE의 신뢰 서명은 다르다. 보안 기능 전체를 끄도록 요구하지 않는다.

## 실제 구성

EXE는 .NET Framework WinExe이며 제품의 PowerShell 설치·업데이트·복구 코드를 재사용한다. 작은 준비 화면에서 패키지를 검증·추출한 뒤 기존 설치창을 연다. 실행 파일 확장자만 바꾼 CMD가 아니다.

EXE 구성은 `MZ` 부트스트랩 + 검증된 ZIP + 64바이트 footer다. footer의 형식은 `<16sQQ32s`이며 magic은 `Q3DSETUPZIPv1`을 16바이트까지 NUL로 채운 값이다. 이어서 stub 크기, ZIP 크기, ZIP SHA256 원문 32바이트를 기록한다. 컴파일된 ZIP 길이·해시와도 대조한다. 임의 ZIP이나 바뀐 manifest를 실행하지 않는다.

ZIP의 경로·대소문자 중복·링크·Windows 예약 이름·추가 파일을 검사하고, manifest의 모든 파일 크기와 SHA256을 확인한 뒤 설치창을 연다. 현재 사용자에 속한 별도 임시 폴더만 사용한다. 설치창이 끝날 때까지 payload를 유지하며, 정리는 생성한 파일에만 제한한다. 앱 설치 폴더·모델·페어링은 정리 대상이 아니다.

동일 사용자 설치 중복 실행은 mutex로 제한한다. 설치창 종료 코드 0은 창이 정상적으로 끝났다는 의미다. 설치 완료로 바꾸어 보고하지 않는다. 실제 설치 성공 여부는 기존 설치기·CUDA·Qt 검사와 완료 기록을 따른다.

## 빌드

Windows의 .NET Framework C# 컴파일러로 빌드한다. 추가 NuGet·설치기 제작 도구·유료 서비스가 필요 없다. 기존 ZIP 빌드와 개인정보 검사를 먼저 수행한다.

```powershell
python -B scripts/release/build_exe_installer.py --zip <Desktop-ZIP> --target pc --version 0.1.2-preview --output <Desktop-Setup.exe>
python -B scripts/release/build_exe_installer.py --zip <Quest-ZIP> --target quest --version 0.1.2-preview --output <Quest-Setup.exe>
```

빌드 소스·컴파일러·ZIP·stub·최종 EXE의 해시가 검증 자료에 남는다. 디버그 PDB나 개인 빌드 경로는 배포하지 않는다. 개인정보 검사기는 EXE 원문에 이어 footer를 해석해 내부 ZIP·중첩 archive·컴파일 리소스까지 검사한다. 알려진 사용자 식별자는 공식 의존성 안에서도 면제하지 않는다.

## 배포 전 완료 기준

- 실제 PC·Quest EXE 컴파일, 아이콘·버전·일반 사용자 권한 manifest 확인
- EXE 원문과 내부 ZIP의 전체 개인정보 검사, 기존 공개 APK·native 해시 보존
- 손상 footer·ZIP·경로 탈출·대소문자 중복·추가 파일·중복 실행 차단
- A 드라이브의 한글·공백 경로에서 실제 추출, PC·Quest GUI 생성 확인
- 실제 PC 새 설치와 기존 설치 업데이트, 개인 파일·모델·페어링·원래 Python 보존
- 실패 원복·업데이트 검증, 실제 CUDA·Qt·PC 캡처→AI→송출 확인
- 대응 소스·라이선스 고지·체크섬 제공, 공개 EXE 익명 전체 다운로드 검증

새 Windows·다른 GPU·네트워크 정책·헤드셋 착용·정량 AV·장시간 검증은 수행한 범위만 별도로 기록한다. 실제 통과 결과와 미검증 범위는 해당 Release의 설치 검증 보고서를 따른다.
