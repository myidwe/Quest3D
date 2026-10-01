# EXE 설치본

## 사용자 흐름

Windows에서는 **Sterevi-Desktop-Setup-0.1.3-preview.exe**를 다운로드해 실행하면 설치창이 열립니다. ZIP을 풀거나 CMD를 실행할 필요는 없습니다. 설치 폴더를 선택하고 **설치**를 누르세요. 기존 설치 폴더를 선택했다면 **업데이트**를 사용합니다. 설치와 연결 설정을 마친 뒤 설치창을 닫고, 이후에는 **Sterevi Desktop** 바로가기로 실행하세요.

Quest에 설치하려면 **Sterevi-Quest-Setup-0.1.3-preview.exe**를 실행하세요. USB 설치창에서 공식 ADB 경로를 지정하고, **기기 검색 → 설치할 Quest 선택 → Quest에 설치** 순서로 진행합니다. 개발자 모드 설정과 USB 디버깅 승인은 사용자가 직접 해야 합니다. Quest APK의 버전은 0.1.3-preview, versionCode는 4입니다. 앱 이름은 바뀌었지만 기존 공개 앱과 같은 패키지와 인증서를 사용하므로 업데이트할 때 설정과 페어링 정보가 유지됩니다. APK를 직접 다운로드해 설치하는 방법도 제공합니다.

PC를 처음 설치할 때는 정해진 버전의 Python, GPU 라이브러리와 모델을 다운로드해야 합니다. 라이브러리는 수 GB, 모델은 약 99 MB입니다. EXE에는 모델, 사용자 설정, 개인 키와 페어링 정보를 넣지 않습니다. 모델을 설치한 뒤에는 AI 처리를 PC에서 실행합니다.

## 권한과 Windows 조건

EXE는 현재 사용자의 권한(`asInvoker`)으로 실행하며 관리자 권한을 자동으로 요청하거나 Windows 정책을 바꾸지 않습니다. 설치 후 **연결 허용**을 누르면 Sterevi의 개인(Private) 네트워크 방화벽 규칙을 확인하고, 필요할 때만 UAC 승인을 요청합니다. 승인을 취소하면 방화벽을 변경하지 않습니다.

EXE 실행에는 Windows x64와 .NET Framework 4.8 이상이 필요합니다. Windows 11과 Windows 10 1903 이후 버전에는 해당 런타임이 포함되어 있으므로 별도 .NET SDK나 컴파일러를 설치할 필요는 없습니다. EXE를 실행할 수 있는 조건과 제품의 지원 범위는 다릅니다. Windows 10의 지원 수명과 다른 PC에서의 동작 여부는 별도로 확인해야 합니다. [Microsoft 공식 런타임 안내](https://learn.microsoft.com/en-us/dotnet/framework/install/on-windows-and-server)

Windows용 코드 서명은 아직 적용하지 않았습니다. SmartScreen이나 회사·학교 PC의 관리 정책에 따라 경고가 나오거나 실행이 차단될 수 있습니다. 공개 APK의 Android 서명은 Windows EXE의 코드 서명과 별개입니다. 설치를 위해 보안 기능 전체를 끄도록 요구하지 않습니다.

## 실제 구성

EXE는 .NET Framework WinExe로 빌드하며, 기존 PowerShell 설치·업데이트·복구 코드를 사용합니다. 작은 준비 화면에서 패키지를 검사하고 푼 뒤 설치창을 엽니다.

EXE는 `MZ` 부트스트랩, 검증된 ZIP, 파일 끝의 64바이트 footer로 구성됩니다. footer의 형식은 `<16sQQ32s`입니다. 형식 식별값(magic)은 `Q3DSETUPZIPv1` 뒤를 NUL로 채운 16바이트이며, 그 뒤에 stub 크기, ZIP 크기와 ZIP SHA256 원문 32바이트를 기록합니다. 컴파일할 때 포함한 ZIP 길이·해시와도 대조하므로 임의의 ZIP이나 manifest가 바뀐 패키지는 실행하지 않습니다.

설치창을 열기 전에 ZIP의 경로, 대소문자만 다른 중복 파일, 링크, Windows 예약 이름과 추가 파일을 검사합니다. manifest에 기록된 모든 파일의 크기와 SHA256도 확인합니다. 현재 사용자의 별도 임시 폴더에 파일을 풀고, 설치창이 닫힐 때까지 유지합니다. 이후에는 EXE가 만든 임시 파일만 정리합니다. 앱 설치 폴더, 모델과 페어링 정보는 정리하지 않습니다.

mutex를 사용해 같은 사용자가 설치창을 중복으로 실행하지 못하도록 합니다. 종료 코드가 0이라는 것은 설치창이 정상적으로 닫혔다는 뜻이며, 설치가 완료되었다는 뜻은 아닙니다. 실제 설치 성공 여부는 설치 도구의 결과, CUDA·Qt 검사와 완료 기록으로 판단합니다.

## 빌드

Windows의 .NET Framework C# 컴파일러로 빌드합니다. 추가 NuGet 패키지, 설치기 제작 도구나 유료 서비스는 필요하지 않습니다. 아래 명령을 실행하기 전에 ZIP을 빌드하고 개인정보 검사를 마쳐야 합니다.

```powershell
python -B scripts/release/build_exe_installer.py --zip <Desktop-ZIP> --target pc --version 0.1.3-preview --output <Desktop-Setup.exe>
python -B scripts/release/build_exe_installer.py --zip <Quest-ZIP> --target quest --version 0.1.3-preview --output <Quest-Setup.exe>
```

검증 자료에는 빌드 소스, 컴파일러, ZIP, stub과 최종 EXE의 해시를 남깁니다. 디버그 PDB와 개인 빌드 경로는 배포하지 않습니다. 개인정보 검사기는 EXE 전체와 footer를 확인하고 내부 ZIP, 중첩 압축 파일과 컴파일된 리소스까지 검사합니다. 알려진 사용자 식별자는 공식 라이브러리 안에 있더라도 검사에서 제외하지 않습니다.

## 배포 전 완료 기준

- PC·Quest EXE의 실제 컴파일과 아이콘·버전·일반 사용자 권한 manifest 확인
- EXE 전체, 내부 ZIP과 직접 다운로드용 APK의 개인정보 검사 및 APK·대응 소스·서명 대조
- 손상된 footer·ZIP, 설치 경로 밖으로 나가는 경로, 대소문자 중복, 추가 파일과 중복 실행 차단
- A 드라이브의 한글·공백 경로에서 실제 파일 추출 및 PC·Quest 설치창 표시 확인
- PC 새 설치와 업데이트 실행 및 개인 파일·모델·페어링 정보·기존 Python 보존 확인
- 업데이트와 실패 시 복원 검증 및 실제 CUDA·Qt·PC 캡처→AI→영상 전송 확인
- 대응 소스·라이선스 고지·체크섬 제공 및 로그인 없이 공개 EXE 전체 다운로드 확인

새 Windows, 다른 GPU, 네트워크 정책, 헤드셋 착용, 영상·음성 동기화 수치와 장시간 사용은 실제로 확인한 범위만 별도로 기록합니다. 통과한 검사와 아직 확인하지 못한 조건은 해당 Release의 설치 검증 보고서에서 확인하세요.
