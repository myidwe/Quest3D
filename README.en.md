# Sterevi

**Your PC screen in stereo 3D**

Turn your existing browser or player screen into stereo 3D with local AI on your PC. Switch **2D ↔ stereo 3D** on the same large virtual screen in Meta Quest.

[한국어](README.md) · **0.1.3-preview**

## 24-second overview

https://github.com/user-attachments/assets/0a0dac44-79db-49f7-911c-ecf5be4d2878

[Vertical video](docs/media/VERTICAL.md) · [Video details and subtitles](docs/media/README.md)

<sub>Real app UI and an actual local depth/left-right processing example. The pop-out car scene is advertising artwork illustrating stereo viewing.</sub>

<details>
<summary>Workflow illustration</summary>

![Windows screen → local AI depth estimation and left/right views → a large stereo screen with visible depth in Quest](docs/assets/quest3d-workflow-v3.png)

<sub>Concept illustration · not an app screenshot or conversion result · [Image provenance](docs/assets/README.md)</sub>

</details>

## Check compatibility

**Windows x64 · NVIDIA Turing (sm75) · Quest 2 / 3 · 16:9 monitor · shared private LAN**

Measured on **RTX 2060 SUPER 8GB / Windows 11**. Memory, encoder support, and performance have not been checked for each other Turing model. Other NVIDIA generations, AMD, and Intel GPUs are currently unsupported. [Support and validation scope](docs/RELEASE_0.1.3_PREVIEW.md)

## Download

| Windows app | Quest app |
|:---|:---|
| **[Desktop Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Desktop-Setup-0.1.3-preview.exe)** | **[Quest Setup EXE](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-Setup-0.1.3-preview.exe)** |
| Install the PC app | Install the Quest app over USB |
| | **[Download APK directly](https://github.com/myidwe/Sterevi/releases/download/v0.1.3-preview/Sterevi-Quest-0.1.3-preview.apk)** · use your existing sideloading tool |

**Run both files on Windows.** The first PC installation requires internet access and downloads several GB of runtime components, GPU libraries, and models. AI then runs on your PC. There are no software fees, subscriptions, or cloud inference charges.

The Windows EXEs do not yet have trusted code signing; warnings or policy blocks may appear. [Installation conditions](docs/EXE_INSTALLERS.md#권한과-windows-조건)

## Install and connect

1. **Desktop Setup → 설치 (Install) → 연결 허용 (Allow connection) → close the installer**
2. Prepare Quest **Developer mode, USB debugging, and ADB** → install with **Quest Setup**
3. **Sterevi Desktop → PC 시작 (Start PC)**; on Quest: **Scan Network → PC → Pair**
4. **Approve the Quest PIN** in the PC app → **Connect** on Quest

**[First installation and connection →](docs/GETTING_STARTED.en.md)** — prerequisites and each step of the first setup.

Daily use: **Sterevi Desktop → PC 시작 (Start PC) → Connect on Quest**

## Features

- Stream existing browsers and programs; switch 2D/3D and adjust Depth and contour stabilization
- Adjust screen size, distance, position, curvature, color, and sharpness; save views
- Quest 2 / 3 quality profiles, H.264 / HEVC, and PC / Quest sound output options

[User guide](docs/DESKTOP_USER_GUIDE.md) · [Troubleshooting](docs/DESKTOP_USER_GUIDE.md#문제-해결) · [All documentation](docs/README.md) · [Questions and bugs](https://github.com/myidwe/Sterevi/issues)

## App screens

**Windows · Display**

<img src="docs/assets/sterevi-desktop.png" alt="Sterevi Windows Display interface" width="640">

| Quest · Home | Quest · Display settings |
|:---:|:---:|
| <img src="docs/assets/sterevi-quest-home.png" alt="Sterevi Quest home interface" width="420"> | <img src="docs/assets/sterevi-quest-settings.png" alt="Sterevi Quest Display settings interface" width="420"> |

<sub>Production app UI renders with privacy-safe sample states · not headset photographs or conversion-quality evidence · [Capture details](docs/assets/README.md)</sub>

## Preview notes

Use the Windows mouse and keyboard for PC input. Thin objects and occluded backgrounds may retain stereo contour differences. DRM or capture-blocked content is not guaranteed to work. Sound defaults to PC output; Quest only requires existing Steam Streaming Speakers. See the [release guide](docs/RELEASE_0.1.3_PREVIEW.md) for remaining checks on other PCs, the latest Quest 2 UI, measured audio synchronization, and long sessions. Detailed technical documents are currently in Korean.

<details>
<summary>How does this relate to OWL3D?</summary>

Sterevi is an independent open-source project for live 2D-to-stereo-3D PC screen viewing. It shares part of the use case offered by [OWL3D Link](https://www.owl3d.com/blog/releasesv203), but is not an OWL3D release or fork and is not affiliated with OWL3D. This does not imply matching features or image quality.

</details>

<details>
<summary>Development, manual installation, and validation</summary>

- [All Release files](https://github.com/myidwe/Sterevi/releases/tag/v0.1.3-preview): manual installation ZIPs, corresponding Source ZIP, checksums, and validation reports
- [Installation, updates, recovery, and removal](docs/DISTRIBUTION.md) · [Setup permissions](docs/SETUP_PERMISSIONS_2026-09-30.md)
- [Architecture, build, and tests](docs/BUILDING.md) · [Contributing](CONTRIBUTING.md) · [Product scope](docs/PRODUCT_SCOPE.md)
- [Privacy remediation](docs/PRIVACY_REMEDIATION_2026-10-01.md) · [Dependency and corresponding source audit](docs/DEPENDENCY_AUDIT_2026-09-30.md)

GitHub's **Code → Download ZIP** provides development source. For the binaries' native corresponding source, use the **Source ZIP** in that Release. Stream FPS and new AI frame generation are different; 60FPS in every scene is not guaranteed.

</details>

Project code: **[GPL-3.0](LICENSE)** · Component and model conditions: [Third-party notices](THIRD_PARTY_NOTICES.md)
