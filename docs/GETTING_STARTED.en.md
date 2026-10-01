# Install and connect

[한국어](GETTING_STARTED.md) · [Documentation index](README.md) · [Project overview](../README.en.md)

This guide is for **0.1.3-preview**. After the first setup, daily use is **PC 시작 (Start PC) → Connect on Quest**. Button names below match the current UI.

## Before you start

- You need **Windows x64 and an NVIDIA Turing (sm75)** GPU. The measured setup is Windows 11 with an RTX 2060 SUPER 8GB. Memory, encoder support, and performance have not been checked for every other Turing model. Other NVIDIA generations, AMD, and Intel GPUs are currently unsupported.
- Prepare a **Quest 2 or Quest 3**, a **16:9 monitor**, and a **private LAN** shared by the PC and Quest. USB is used to install the Quest app.
- The first PC installation requires internet access and downloads several GB of pinned runtime components, GPU libraries, and models. AI processing then runs on your PC.

The Windows EXEs do not yet have trusted code signing. SmartScreen or managed PC policies may warn or block them. See [Windows installation conditions](EXE_INSTALLERS.md#권한과-windows-조건) and the [actual validation scope](RELEASE_0.1.3_PREVIEW.md).

## 1. Install the Windows app

**[Download Desktop Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Desktop-Setup-0.1.3-preview.exe)**

1. Open the EXE, choose an installation folder, and press **설치 (Install)**. Wait for the downloads and installation to finish.
2. When installation completes, press **연결 허용 (Allow connection)**. Confirm the Windows approval prompt if it appears.
3. **Close the installer**, then open **Sterevi Desktop** from the desktop or Start menu.
4. Before starting the stream, choose Quest 2 / Quest 3 under **Settings → Quality → Headset** and check the selected monitor.

## 2. Install the Quest app

**[Download Quest Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-Setup-0.1.3-preview.exe)** — run this file on the Windows PC too.

1. Follow [Meta's official device setup](https://developers.meta.com/vr/documentation/native/android/mobile-device-setup/) to meet the developer account/team requirements and enable **Developer mode** for Quest in the Meta Horizon app. Install the Windows **Oculus ADB Drivers** following that same official guide.
2. Connect the PC and Quest with a USB data cable, then approve **USB debugging** inside the headset.
3. Download and extract [Google's official Android Platform Tools](https://developer.android.com/tools/releases/platform-tools).
4. Open the Quest Setup EXE and select **adb.exe** from Platform Tools. Press **기기 검색 (Find devices) → select the Quest to install → Quest에 설치 (Install on Quest)**.
5. In the headset's app library, open **Unknown Sources → Sterevi**. The app library location can vary by Horizon OS version.

Install this update to display the new Sterevi name. The public package and signing identity are unchanged; existing settings and pairing are retained. Do not uninstall the old app first.

### Direct APK installation

Prefer your own sideloading tool? [Download the APK](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-0.1.3-preview.apk) alongside the Quest Setup EXE. Developer mode and USB debugging approval still apply. With Google's Platform Tools, use:

```powershell
adb install -r "Sterevi-Quest-0.1.3-preview.apk"
```

`-r` updates the existing public app without deleting its data. A new install still requires the initial Pair/PIN flow below; an existing saved PC can use Connect. The APK is identical to the one bundled in Quest Setup. Check `SHA256SUMS.txt` in the same Release.

## 3. Connect for the first time

1. Connect the PC and Quest to the same router's private LAN. On a trusted home network, check that the Windows network profile is **Private**.
2. Press **PC 시작 (Start PC)** in the PC app and wait for the video to be ready.
3. On Quest, choose **Select Server → Scan Network → select the discovered PC → Pair**.
4. Enter the four-digit PIN shown on Quest under **Connection → 새 Quest 연결 (Connect a new Quest)** in the PC app. Press **연결 승인 (Approve connection)**. Keep the Quest PIN screen open until approval completes.
5. If Quest does not connect automatically, press **Connect**.

If the PC does not appear, use **+** on Quest to enter the address shown in the PC app. See [connection troubleshooting](DESKTOP_USER_GUIDE.md#문제-해결) if the connection still fails.

## Daily use

**Open Sterevi Desktop → PC 시작 (Start PC) → Connect to the saved PC on Quest**

After connecting, adjust **Mode · 2D / 3D** and **Depth**. Press **PC 중지 (Stop PC)** when you finish. See the [display, quality, and shutdown guide](DESKTOP_USER_GUIDE.md).

Sound defaults to **PC** output. Quest only requires **Steam Streaming Speakers** to be installed and active already. See [sound output options](DESKTOP_USER_GUIDE.md#sound--소리-출력).

See the [detailed installation guide](DISTRIBUTION.md) for updates, recovery, and installation errors; the [current release guide](RELEASE_0.1.3_PREVIEW.md) for support and remaining validation; and the [current Release](https://github.com/myidwe/Sterevi/releases/tag/v0.1.3-preview) for checksums and validation reports. These detailed documents are currently in Korean.
