# Portable authored canine assets

`rig.js`, `animal-body.js`, `face-geometry.js`, `mouth-shapes.js` and
`head-binding.js` are project-authored canine geometry/control code reused with
the user's authorization for this Skill. Their geometry is biped-staged canine
art, not scanned anatomy. No source media, shot motion, learned weights, private
paths or historical per-frame corrections are included. This notice does not
grant a separate public open-source license for the project-authored assets.

`scene.mjs` is the portable image-plane adapter. The reviewed source stage used
the template's expression controls and then replaced its limb meshes from a
separate fixed-shape skeleton; the template's default 1.65 arm segments were
not its final staged proportions. This adapter preserves that separation.
It estimates one shoulder/hip width and upper/lower limb length per side from
the current clip's reliable projected segment medians, freezes them for the
entire clip, and uses `projected-limbs.mjs` for bounded two-link image-plane IK.
No old shot's poses, body measurements or frame corrections are carried over.
These are screen-space staging estimates, not anatomical lengths or recovered
depth. Actual mesh lengths and projection/reach residuals are recorded.
Body/head/face/hand/foot controls remain separate. The output does not establish
real 3D depth, a ground plane or contact, and still requires human review.

`vendor/three.module.js` and `vendor/three.core.js` are bundled Three.js r181
(npm version 0.181.0), Copyright 2010–2025 Three.js Authors, distributed under
the MIT License in `vendor/LICENSE`. Original upstream:
https://github.com/mrdoob/three.js/tree/r181/build

`manifest.json` pins every runtime asset by SHA-256. The existing upstream
license and the unmodified local upstream build headers are retained.
