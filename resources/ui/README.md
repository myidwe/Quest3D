# Pinned UI assets

These are unmodified upstream assets for the Windows and Quest interfaces. They are not a chosen UI design, and adding them does not change the running apps.

## Fonts

Pretendard **v1.3.9**, commit `5c41199ea0024a9e0b2cb31735265056e5472d76`, from the official [Pretendard repository](https://github.com/orioncactus/pretendard/releases/tag/v1.3.9).

- `fonts/Pretendard-Regular.otf`: static weight 400
- `fonts/Pretendard-Medium.otf`: static weight 500
- `fonts/Pretendard-SemiBold.otf`: static weight 600
- `fonts/OFL-Pretendard.txt`: verbatim upstream SIL Open Font License 1.1 and copyright notice

These are standard static OpenType/CFF files from `packages/pretendard/dist/public/static`, not a generated subset or a variable font. Their OS/2 weights were verified, and none has an fvar table. Qt 6.8.3 resolves all three to the family `Pretendard` with the exact named style/weight and Korean glyph support. Explicitly request Regular/400 for body text. Use Medium/500 and SemiBold/600 only for actual hierarchy, rather than setting every UI element to the same heavy weight. Do not synthesize bold when a supplied weight is available.

## Icons

Lucide **0.468.0**, commit `f12b0de177fbc2a6795e99be065887e72b237123`, from the official [Lucide repository](https://github.com/lucide-icons/lucide/releases/tag/0.468.0).

The 18 requested icons are stored in `icons/` with their official names. `icons/LICENSE-Lucide.txt` is the verbatim upstream ISC license, including the upstream attribution for Feather-derived portions. Every SVG keeps its original 24×24 viewBox and `stroke="currentColor"`. No geometry or stroke width has been edited. Qt 6.8.3 SVG loading/rendering passed for all 18 files.

For a fixed-color renderer, replace `currentColor` in memory when creating the icon (for example, before passing SVG bytes to QSvgRenderer), or generate a separately identified derived asset. Keep the pristine originals unchanged. In Godot, importing a black SVG and setting CanvasItem modulation to white will not turn its black pixels white; provide a separately colored import when necessary. Consumers should keep sufficiently large hit areas independently of the visible 18–24px icon.

## Reproducibility and distribution

`ASSET_MANIFEST.json` records the fixed source commits, official download URL, source path, exact size, SHA256 and Git blob SHA-1 for every upstream file. Each downloaded file was compared against the official Git tree blob at the pinned commit. This preserves both the source revision and actual bytes.

Include the two license files with app/source distributions. Pretendard has a Reserved Font Name; these font files are unmodified. Do not bundle developer data, signing keys or the preview files as though they were runtime dependencies.

Local acquisition/validation evidence: `artifacts/design-assets-20260911/fetch_assets.py`, `verify_assets.py`, `verification.json`, and `pinned-assets-preview.png`. The preview renders the actual assets; it is a typography/icon reference, not a new product screen.
