# Quest3D

View your Windows desktop on a large Meta Quest screen and switch between 2D and stereo 3D using local AI.

**Windows / NVIDIA Turing · 0.1.1-preview**

[한국어](README.md)

View your existing browser and applications on a large screen, switching between 2D and stereo 3D. AI runs on the PC. This first public Preview has limited hardware support. The [release guide](docs/RELEASE_0.1.1_PREVIEW.md) and `release-validation.json` separate verified behavior from remaining hardware checks.

## Install

**[Windows download](https://github.com/myidwe/Quest3D/releases/download/v0.1.1-preview/Quest3D-Desktop-0.1.1-preview.zip)** · **[Quest download](https://github.com/myidwe/Quest3D/releases/download/v0.1.1-preview/Quest3D-Quest-0.1.1-preview.zip)** · [Release](https://github.com/myidwe/Quest3D/releases/tag/v0.1.1-preview)

Download matching Desktop and Quest ZIPs. **Code → Download ZIP** contains developer source, not the installer. The same Release provides complete native corresponding sources, SHA256 checksums and validation results.

1. Extract the entire Desktop ZIP and open **Install-Quest3D.cmd → 설치 (Install) → 연결 허용 (Allow connection)**. Windows requests administrator approval only when needed for the app's firewall setup.
2. Enable Quest developer mode and approve USB debugging. Open **Install-Quest.cmd**, select official Google Platform Tools' `adb.exe`, search devices and select your Quest.
3. Connect PC and Quest to the same private LAN.
4. Open **Quest3D Desktop → PC 시작 (PC start)**.
5. On Quest, **Scan Network → select PC → Pair**. Approve the PIN in the PC app.
6. Subsequent use: **PC start → Quest Connect**.

The first installation downloads Python, pinned GPU libraries and the default model. AI runs locally after setup; there are no subscription or cloud inference fees. End users do not need Codex, WSL or native build tools. Read the install guide for developer-mode, USB and supported-GPU requirements.

## Features

- Existing browser/application display on the selected monitor
- 2D/3D switching, fine Depth control and contour stabilization
- Depth Anything V2 Small default; optional Distill Any Depth Small comparison
- Standard / Quality · Preview depth inference modes
- Quest screen size, distance, position, curvature, color, sharpness and saved views
- Level alignment by default; explicit Free alignment; movable settings panel
- H.264/HEVC, Quest 2/3 profiles, PC start/stop, PIN pairing and diagnostics
- PC sound by default; Quest only / PC + Quest options

Quest only requires an existing active **Steam Streaming Speakers** device. Quest3D does not install or redistribute that driver. PC + Quest captures the existing PC output without changing its default device.

## Current support

| Component | Scope |
|---|---|
| Host | Windows x64; tested on Windows 11 |
| GPU | CUDA path currently restricted to NVIDIA Turing `sm75` |
| Tested GPU | RTX 2060 SUPER 8 GB |
| Headset | Meta Quest 2 / Quest 3 |
| Network | Same private LAN; USB is for installation/diagnostics |
| Monitor | Currently 16:9 |
| Quest 2 stream | 1920×1080 per eye; 3840×1080 full SBS |
| Quest 3 stream | 2048×1152 per eye; 4096×1152 full SBS, HEVC |

Individual RTX 20 / GTX 16 models have not all been validated. Other NVIDIA generations, AMD/Intel GPUs and ARM Windows are not current supported targets. A faster or newer GPU does not automatically pass the present kernel architecture checks.

## Performance and limitations

On the development RTX 2060 SUPER, September 22 PC processing measurements produced **38.66 new 3D frames/s** with DAD Standard and **34.64** with Quality · Preview over 60-second runs. These are scene-specific PC results, not Quest received FPS or minimum performance guarantees. Repeating output at 60/s is different from generating 60 new stereo frames/s.

Thin objects and occluded backgrounds can retain stereo contour artifacts. Quality is not uniformly better across scenes. Reduce Depth or switch to 2D if uncomfortable. Protected/DRM content may not be capturable. Latest Quest 2 UI/audio retesting, wearer listening, A/V offset, long runs and installation on another PC remain unverified.

PC interaction from Quest, a built-in media player, in-place region-only 3D conversion, dedicated A/V synchronization correction and complete spatial reconstruction are outside the current product scope.

## Development and license

Pinned baseline: Python 3.12.6, PyTorch 2.7.1+cu126, Qt 6.8.3. Operational capture is `wc_cuda 0.1.2+quest2`, distinct from the `+quest1` wheel referenced in `uv.lock`. Source checkout alone does not supply the native host/capture binaries.

Project code is **GPL v3**; third-party components retain their own licenses. The exact pinned DAv2 Small and DAD Small weight revisions are Apache-2.0 under their official model cards. This does not extend to every size or model.

[Install guide](docs/DISTRIBUTION.md) · [Build instructions](docs/BUILDING.md) · [Contributing](CONTRIBUTING.md) · [Notices](THIRD_PARTY_NOTICES.md) · [Release preparation](docs/GITHUB_PUBLICATION.md)
