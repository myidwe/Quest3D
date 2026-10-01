# Third-party notices

Quest3D project code is licensed under GNU GPL version 3 (LICENSE). Dependencies retain their licenses and copyrights. No upstream project, Meta, NVIDIA, Netflix or Laftel endorses this app.

| Component | Pin | License |
|---|---|---|
| Sunshine | cb72dffa3233c5815cd5ba88f09f049dd679ba75 plus the supplied Quest3D modifications; Preview host 2026.930.1 | GPL-3.0, runtime LICENSE.txt and exact corresponding source |
| Nightfall | 2c2162af9738dadb32e441a48255cef65bf7dd56 plus Quest3D modifications | GPL-3.0, source LICENSE |
| Godot / godot-cpp | 5b4e0cb0fd279832bbdd69fed5354d4e5ad26f88 / 05057de73de4b99f114d36c40d84ca46926c0e25, patched | MIT and bundled notices |
| Godot OpenXR Vendors | 5.0.0-stable source 6a04c8632140f7dc14670e5564fd473464047a15; newly rebuilt with public Khronos headers | MIT, original license and contributors supplied. This Preview vendor build does not select Meta preview SDK headers. |
| OpenXR vendor submodules | godot-cpp 58d1de720b8ffe9f8ffcdfe3a85148582cfd2e74 / OpenXR SDK ba4aec9686cb94c99a55f7ceba9768e9e35525c2 | Original source archives and license texts supplied with the Quest source. |
| Khronos OpenXR loader | 1.1.54, Android Maven runtime and exact source release | Apache-2.0 with original notices and bundled component licenses, including jsoncpp MIT. |
| Android NDK libc++ | NDK r29, 29.0.14206865 | NDK NOTICE and NOTICE.toolchain, including LLVM exception/license notices, supplied. This ordinary compiler runtime is reused from the pinned toolchain. |
| Quest native and Android dependencies | Exact ABI-bound native recipes and source archives; 35 Maven modules / 37 runtime artifacts | FFmpeg 7.1.2 is GPL-enabled; Moonlight, ENet, curl, OpenSSL, Opus, SIMDe and zlib retain their licenses. Android module source JARs and discovered LICENSE/NOTICE texts accompany the source; Apache-2.0 text is included. |
| Moonlight-common-c | Host/Quest submodule and port pins | GPL-3.0 and dependency notices |
| wc_cuda | 6f6c6eaed91f36f0e937f1da92cf5cfc35a9bfcc + quest1/quest2 patches | MIT, copyright (c) 2026 nagadomi, licenses/wc-cuda-MIT.txt |
| Depth Anything V2 source | a561b849ebae10a6f5ef49e26c83cbbcd36c71bf | Apache-2.0, included license |
| Depth Anything V2 Small weights | 03876f8651c73a60fe4c2c48294e09fcb6838fcf | Apache-2.0 per exact model card. Downloaded by installer, not in ZIP. No Base/Large/Giant model is selected. |
| Distill Any Depth Small weights (optional) | xingyang1/Distill-Any-Depth, 38095a41cca1e28a28e8bb6372c68df721455a2d, small/model.safetensors | Apache-2.0 per pinned model card. SHA256 56a173c0e1b5045bf6296a5c1fb16eace0bbde2eddc24b37532cb1774ac09caa. Explicit optional download, not in ZIP. Runtime reuses the pinned Apache-2.0 Depth Anything V2 DPT implementation. DAD upstream training source 6d8f415392eafb49c96a38cc4dedbd09a1607f50 is MIT and is not required or bundled for this runtime. |
| safetensors | 0.6.2, exact Windows wheel hash in uv.lock | Apache-2.0; original license remains in the installed wheel's dist-info/licenses directory. |
| PowerShell | 7.6.5 | MIT, LICENSE.txt and ThirdPartyNotices.txt retained |
| uv | 0.10.12 | MIT OR Apache-2.0, both texts included |
| Python | 3.12.6 x64 from python.org | PSF-2.0, installer retains bundled notices |
| PySide6-Essentials / shiboken6 / Qt | 6.8.3, exact Windows wheel hashes in uv.lock | PySide/shiboken wheel metadata: LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only. Community Qt modules have their applicable LGPL/GPL and third-party licenses. License texts are in licenses/Qt-*.txt; no commercial package is selected. |
| Pretendard | v1.3.9, 5c41199ea0024a9e0b2cb31735265056e5472d76 | SIL Open Font License 1.1, resources/ui/fonts/OFL-Pretendard.txt. Three unmodified static OTF weights, not a renamed/modified font. |
| Lucide icons | 0.468.0, f12b0de177fbc2a6795e99be065887e72b237123 | ISC with upstream Feather attribution, resources/ui/icons/LICENSE-Lucide.txt. Exact sources and SHA-256 in resources/ui/ASSET_MANIFEST.json. |
| PyTorch / torchvision | 2.7.1+cu126 / 0.22.1+cu126 | BSD-style project licenses plus CUDA/bundled terms in downloaded wheel license files |
| NumPy / Pillow / OpenCV / PyAV / MSS / psutil | Exact versions/hashes in uv.lock | Their project and bundled dependency licenses remain in installed wheels |
| zlib | Runtime DLL | zlib, licenses/zlib.txt |

The Preview host is the newly rebuilt executable with SHA-256 `86eb2ee5e3177a892f15ecd5ba869b27d3e1b9131848b1adacf9a25301767d42`. Its modified Sunshine source archive, all initialized submodule inputs, exact dependency sources/recipes, toolchain lock and executed build helpers are supplied together. The host source, native/web build and original dependency notices have been checked against this executable. The preserved historical development executable and its partial source snapshot are separate inputs and are not claimed to be this Preview binary's corresponding source.

The new Quest Preview APK uses freshly built project, stream, XR, modified Godot and public-header OpenXR vendor inputs. Compatible official editor/template, Android Maven and NDK inputs remain version/hash pinned. Their applicable source and original notices are supplied; this is not a claim that every untouched tool or compiler runtime was recompiled. The GPL-enabled native stream is part of the combined GPL app. Historical preview-header references retained in source evidence do not describe the headers selected for the new vendor binary.

Detailed files in the downloads:

- Desktop ZIP: `artifacts/host/runtime-public/notices/THIRD_PARTY_NOTICES.md`, `dependency-notices.json` and the adjacent original notice trees. They include the actual host-linked libraries and production web dependencies.
- Quest ZIP: `notices/quest/NOTICES.md` and `notices/quest/licenses/`, including Godot, OpenXR, NDK, native dependency and Android module texts.
- Matching Source ZIP: `sources/sunshine/SOURCE.md`, `sources/sunshine/provenance.json` and `sources/sunshine/notices/`; `sources/quest/SOURCE_BUILD_NOTES.md`, `sources/quest/source-manifest.json`, `sources/quest/NOTICES.md` and `sources/quest/licenses/`. These identify the corresponding binary, exact build inputs and supplied source archives.

Use the Source ZIP attached to the **same Release** as the downloaded Desktop/Quest ZIPs. GitHub's automatically generated repository source ZIP is not a substitute for this complete native source/dependency package. The release review compares the actual distributed notice bytes with their corresponding Source ZIP bytes. Build/source/notice checks are separate from installation, headset, audio and long-run verification; the Release notes state those practical limits. No signing private key, pairing record or user's media is included. APK signing certificates are public verification material.

The installer downloads unmodified, hash-pinned public PyPI Qt wheels into an ordinary virtual environment; Qt DLLs are dynamically loaded and are not embedded into a locked executable. Their original wheel metadata/files are retained, and the user can inspect or replace the libraries. The distributed Quest3D Python/QML source is GPLv3. PySide/shiboken source is the upstream v6.8.3 tag; Qt module sources use v6.8.3. The listed wheel and license pins remain unchanged by the native Preview rebuild.

Qt references: [Qt for Python Community licensing](https://doc.qt.io/qtforpython-6.8/commercial/index.html), [PySide6-Essentials 6.8.3](https://pypi.org/project/PySide6-Essentials/6.8.3/), [PySide/shiboken exact source](https://github.com/qtproject/pyside-pyside-setup/tree/v6.8.3), [Qt 6.8 licenses and third-party components](https://doc.qt.io/qt-6.8/licensing.html). Exact downloaded license file hashes/source URLs are retained in licenses/Qt-SOURCES.json.

References: [exact Small model card](https://huggingface.co/depth-anything/Depth-Anything-V2-Small/blob/03876f8651c73a60fe4c2c48294e09fcb6838fcf/README.md), [Python 3.12.6 SPDX](https://www.python.org/ftp/python/3.12.6/python-3.12.6-amd64.exe.spdx.json), [GNU GPL version 3](https://www.gnu.org/licenses/gpl-3.0.html).

Optional model reference: [exact DAD Small model card](https://huggingface.co/xingyang1/Distill-Any-Depth/blob/38095a41cca1e28a28e8bb6372c68df721455a2d/README.md). Model files are verified against config/models.json before loading; a different checkpoint does not inherit this notice automatically.
