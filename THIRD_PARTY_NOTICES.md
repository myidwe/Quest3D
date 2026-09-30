# Third-party notices

Quest3D project code is licensed under GNU GPL version 3 (LICENSE). Dependencies retain their licenses and copyrights. No upstream project, Meta, NVIDIA, Netflix or Laftel endorses this app.

| Component | Pin | License |
|---|---|---|
| Sunshine | cb72dffa3233c5815cd5ba88f09f049dd679ba75 plus Quest3D modifications | GPL-3.0, runtime LICENSE.txt and corresponding source |
| Nightfall | 2c2162af9738dadb32e441a48255cef65bf7dd56 plus Quest3D modifications | GPL-3.0, source LICENSE |
| Godot / godot-cpp | 5b4e0cb0fd279832bbdd69fed5354d4e5ad26f88 / 05057de73de4b99f114d36c40d84ca46926c0e25, patched | MIT and bundled notices |
| Godot OpenXR Vendors | 5.0.0-stable, b68135657f64fea782cac3efe3e5cef7a4de13e4215f1b621ce6a49fe32592eb | MIT project; vendor components have separate terms. Actual APK components and complete notices remain a release gate; see docs/DEPENDENCY_AUDIT_2026-09-30.md. |
| OpenXR loader / Android NDK libc++ | Present in the current APK | Khronos loader and NDK component notices must be matched to actual build inputs before binary publication; not relicensed as Quest3D code. |
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

The stable host historical 49-file modified source snapshot is kept separately from later working-tree experiments. Pinned upstream/submodule tars, toolchain locks, capture patches and build scripts accompany the source candidate. Source/binary correspondence and clean installation must be checked before publication. No signing private key, user certificate, pairing record or user's media is included.

The installer downloads unmodified, hash-pinned public PyPI Qt wheels into an ordinary virtual environment; Qt DLLs are dynamically loaded and are not embedded into a locked executable. Their original wheel metadata/files are retained, and the user can inspect or replace the libraries. The distributed Quest3D Python/QML source is GPLv3. PySide/shiboken source is the upstream v6.8.3 tag; Qt module sources use v6.8.3. A public release still requires the source/binary correspondence checks recorded above; this notice is not a claim that those checks passed.

Qt references: [Qt for Python Community licensing](https://doc.qt.io/qtforpython-6.8/commercial/index.html), [PySide6-Essentials 6.8.3](https://pypi.org/project/PySide6-Essentials/6.8.3/), [PySide/shiboken exact source](https://github.com/qtproject/pyside-pyside-setup/tree/v6.8.3), [Qt 6.8 licenses and third-party components](https://doc.qt.io/qt-6.8/licensing.html). Exact downloaded license file hashes/source URLs are retained in licenses/Qt-SOURCES.json.

References: [exact Small model card](https://huggingface.co/depth-anything/Depth-Anything-V2-Small/blob/03876f8651c73a60fe4c2c48294e09fcb6838fcf/README.md), [Python 3.12.6 SPDX](https://www.python.org/ftp/python/3.12.6/python-3.12.6-amd64.exe.spdx.json), [GNU GPL version 3](https://www.gnu.org/licenses/gpl-3.0.html).

Optional model reference: [exact DAD Small model card](https://huggingface.co/xingyang1/Distill-Any-Depth/blob/38095a41cca1e28a28e8bb6372c68df721455a2d/README.md). Model files are verified against config/models.json before loading; a different checkpoint does not inherit this notice automatically.
