# Introduction illustration

[quest3d-workflow-v3.png](quest3d-workflow-v3.png) is the current introduction illustration: a smaller Windows input screen, an enlarged central local depth and left/right view stage, and a large stereo screen in Quest. The larger center makes depth estimation and stereo generation easier to see. The near rock, middle-distance forest/lake, and distant mountains are shown as separated planes to explain depth in a flat illustration. These plane edges are explanatory graphics, not part of the app's rendered video.

[quest3d-workflow.png](quest3d-workflow.png) and [quest3d-workflow-v2.png](quest3d-workflow-v2.png) are preserved earlier versions. V2 clarified the right-hand screen's depth. V3 enlarges the center and reduces the left monitor. Each revision used the built-in ImageGen tool on 2026-10-01 with the preceding generated image as its edit target.

Generated on 2026-10-01 with the built-in ImageGen tool. The landscape, depth thumbnail, stereo pair, hardware, and virtual screen are illustrative. They are **not actual app screenshots, model outputs, or evidence of image quality**. No OWL3D screenshots, logos, or other assets were used. No external reference images were supplied; edits used only this project's previously generated images.

All three PNGs are included exactly as returned by ImageGen, without subsequent pixel edits. These documentation assets are distributed with the project under [GPL-3.0](../../LICENSE); this notice does not claim exclusive rights to AI-generated material. App and model license terms remain in [Third-party notices](../../THIRD_PARTY_NOTICES.md).

## Generation prompt

```text
Use case: ads-marketing
Asset type: original wide GitHub introduction illustration for Quest3D, an independent open-source Windows-to-Meta-Quest live 2D-to-stereo-3D app.
Primary request: make a refined, immediately understandable product concept illustration showing ONE existing PC screen becoming stereoscopic on a large virtual Quest screen through local AI. This is a concept illustration, not a screenshot or measured conversion sample.
Style/medium: premium editorial 3D product illustration, precise clean typography, matte charcoal/navy background, soft light gray hardware, restrained icy cyan accent, subtle studio lighting and carefully balanced negative space. Quiet sophistication, credible developer product, no neon cyberpunk or busy dashboard.
Composition: a wide, low banner about 3:1 aspect ratio (roughly 1800 x 600). Three clearly separated stages arranged left to right, connected by two understated arrows. Left: simple desktop monitor with a generic browser window showing an ORIGINAL cinematic landscape with distant mountains, middle-distance pine trees and a foreground rock. Center: small local AI processor tile and a restrained depth-map thumbnail of the same scene with two small left/right view tiles. Right: a larger floating 16:9 virtual cinema screen showing THE SAME landscape, subtly suggesting foreground/middle/background layers entirely within the screen boundary, accompanied by a neutral unbranded VR headset. All three stages share the same recognizable original scene.
Text (verbatim): only these three clear stage labels, centered below their respective stages: "Windows screen", "Local AI", "Quest 2 / 3". Small "L" and "R" labels permitted on the paired central tiles. No headline or paragraphs.
Constraints: viewer watches a flat cinema screen with stereo depth; do not depict walkable reconstruction, objects leaving the screen, red/cyan anaglyph, 3D glasses, controller mouse input, cloud processing, fake app controls, performance claims, brand logos or trademarks beyond the provided stage words. No OWL3D branding/assets, no recognizable film/anime characters, no personal screens or account data. Crisp legible text, all content comfortably inside image bounds.
```

## V2 editing prompt

```text
Use case: precise-object-edit
Edit target: the supplied Quest3D product introduction banner.
Primary request: ONLY improve the RIGHT Quest display so its stereo 3D depth is immediately obvious in a normal 2D illustration. The current right display looks like an enlarged flat photograph.
Preserve exactly: the left Windows monitor and its image, the central Local AI icon, depth thumbnail and L/R tiles, both arrows, all three stage labels and their spelling, the headset, overall banner proportions, typography, dark palette, and lighting.
Change only the landscape presentation within the right display: make a sophisticated stereoscopic cinema cutaway with clearly separated foreground, middle ground and background. Keep the same recognizable mountain-lake-pine-rock scene. The large foreground rock and closest pine boughs occupy an unmistakably nearer front plane; lake shoreline and forest occupy a second plane set behind it; mountains and sky occupy a far plane. Show modest physical gaps between these three depth planes via oblique perspective, clean occlusion, thin translucent cyan plane edges and very subtle inter-plane shadowing. The near rock should feel close to the viewer and the mountains far inside the virtual screen. The display remains ONE large bounded virtual screen; use the available right display area efficiently so it reads as a deep window, not a flat screenshot. The depth-layer explanation must be clear and materially stronger than the supplied image while looking polished.
Constraints: a concept illustration of viewing stereoscopic content on a screen, not a walkable reconstructed environment. No objects reaching across other stages; no red/cyan anaglyph, doubled ghost edges, blur, extra text, extra UI, new logos, new brand names, motion marks, or performance claims. Keep the right label exactly "Quest 2 / 3".
```

## V3 editing prompt

```text
Use case: precise-object-edit
Edit target: the supplied Quest3D workflow concept banner (V2).
Primary request: rebalance the illustration so the CENTER local AI depth-to-stereo process becomes the dominant explanatory stage, and the LEFT Windows monitor becomes visibly smaller. The user wants to understand how a flat picture is made into stereo; tiny center thumbnails currently undersell this.
Composition: preserve the wide 3:1 banner. Allocate approximately 22% of width to the left PC stage, 42% to the center AI stage, and 34% to the right Quest stage, with small clean arrow gaps. The center must be substantially larger, not a minor enlargement.
Left: reduce the entire monitor/keyboard/mouse group by roughly 25-30%; keep the same original landscape and generic browser frame. It should read as a simple flat input screen, secondary to the conversion.
Center: enlarge the depth-map landscape and the L/R view pair by roughly 60-80%, filling most of the central area. Keep a restrained compact AI processor/brain icon above. Use a large grayscale depth-map plane of the same near-rock/mid-forest-lake/far-mountains scene, with subtly staggered near/mid/far depth layers visibly separated in perspective; below it, show two generously sized crisp left/right view tiles of that SAME scene with slightly different viewpoints, labeled "L" and "R". Make the visual explanation strong and easy to read at GitHub README size. Avoid adding steps or busy decorations.
Right: retain the Quest headset and the single virtual display with the clearly separated near rock, forest/lake and mountain planes from V2. It may become modestly narrower to accommodate the center. Depth must remain visible. Keep content as bounded screen stereo viewing, not a walkable environment.
Preserve: the original recognizable landscape, coherent three-stage left-to-right story, two arrows, premium editorial 3D rendering, matte navy/charcoal background, restrained icy cyan accent, soft-gray headset, typography and lighting. All three labels remain the SAME font size and exactly "Windows screen", "Local AI", "Quest 2 / 3". Center its "Local AI" label below the enlarged center. Keep all edges and labels inside image bounds.
Constraints: concept illustration, not actual screenshots or model output. No new text other than permitted L/R labels, no red/cyan anaglyph, blurry or ghosted contours, clouds for processing, performance claims, new branding, personal data, or controllers. No extra objects outside the three-stage workflow. Make the center enlargement and left reduction unmistakable.
```

## Sterevi app interface captures · 2026-10-01

These are **actual production interface renders with sample state**, not generated mockups. The Windows capture uses the production Qt Quick/QML window; the Quest home and Display captures use production Godot controls, fonts, icons and theme rendered in a PC viewport. They are not headset photographs, live-stream measurements or model/conversion quality evidence. No service videos, personal desktop contents, PINs or actual host/device names are shown. The documentation-only IP address in the Windows sample is not a user's address.

- [Windows Display](sterevi-desktop.png): idle privacy-safe state, Quest 3 profile and sample monitor. Production `resources/desktop/Main.qml`, `src/quest3d/desktop_qt_adapter.py`.
- [Quest home](sterevi-quest-home.png): production welcome screen with the new Sterevi title. Sample environment, rendered on PC.
- [Quest Display settings](sterevi-quest-settings.png): production menu and sample values for mode, size, placement and presets. Rendered on PC.

Images are copied unchanged from the application renderer outputs, with no AI retouching, recoloring, resizing or compositing. Product interface and inherited assets retain the project's GPL and third-party terms in [the notices](../../THIRD_PARTY_NOTICES.md). The introduction banner above remains a separate generated concept illustration.

| Capture | SHA-256 | Bytes |
|---|---|---:|
| `sterevi-desktop.png` | `8699338093b2e8b3d121fe12c5f7eab091fbb950e6d87e12617876493bdd472a` | 47674 |
| `sterevi-quest-home.png` | `d2e7879448d5013d8c534e788275f0232f715d2f84be3305b4138c5aeb4958d2` | 2026711 |
| `sterevi-quest-settings.png` | `4ab52e845b52e7eecc247638cc63eea6b6dd1a6594ab7d3c77dc2d584f1af483` | 72268 |
