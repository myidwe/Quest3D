# Introduction illustration

[quest3d-workflow-v2.png](quest3d-workflow-v2.png) is the current introduction illustration: an existing Windows screen, local depth estimation and left/right view synthesis, and a large stereo screen in Quest. The near rock, middle-distance forest/lake, and distant mountains are shown as separated planes to make stereo depth easier to understand in a flat illustration. The plane edges are explanatory graphics, not part of the app's rendered video.

[quest3d-workflow.png](quest3d-workflow.png) is the preserved first version. V2 was edited from this original with the built-in ImageGen tool on 2026-10-01 to clarify the right-hand screen's depth.

Generated on 2026-10-01 with the built-in ImageGen tool. The landscape, depth thumbnail, stereo pair, hardware, and virtual screen are illustrative. They are **not actual app screenshots, model outputs, or evidence of image quality**. No OWL3D screenshots, logos, or other assets were used. No external reference images were supplied; the V2 edit used only this project's generated first version.

Both PNGs are included exactly as returned by ImageGen, without subsequent pixel edits. These documentation assets are distributed with the project under [GPL-3.0](../../LICENSE); this notice does not claim exclusive rights to AI-generated material. App and model license terms remain in [Third-party notices](../../THIRD_PARTY_NOTICES.md).

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
