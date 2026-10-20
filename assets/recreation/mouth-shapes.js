export const MOUTH_SHAPES_BUILD_ID = '86243aa74bc73dac5bc2da66a94a542433b67f4e8dd770ff480715d8ee642a54';
import * as T from './vendor/three.module.js';

const clamp = (value, low, high, fallback) => Number.isFinite(value) ? Math.max(low, Math.min(high, value)) : fallback;
const N = 64;
const smooth = (a, b, value) => { const t = clamp((value - a) / (b - a), 0, 1, 0); return t * t * (3 - 2 * t); };

// A short dark canine eyelid edge, only visible during the explicitly driven
// closing part of the existing lid motion. No eyelashes or autonomous blink.
export function createEyelidCrease(parent, mat) {
  const U = 14, geometry = new T.BufferGeometry();
  geometry.setAttribute('position', new T.BufferAttribute(new Float32Array((U + 1) * 4 * 3), 3));
  const indices = [];
  for (let i = 0; i < U; i++) for (let q = 0; q < 4; q++) {
    const a = i * 4 + q, b = (i + 1) * 4 + q, c = (i + 1) * 4 + (q + 1) % 4, d = i * 4 + (q + 1) % 4;
    indices.push(a, b, d, b, c, d);
  }
  geometry.setIndex(indices);
  const mesh = new T.Mesh(geometry, mat); mesh.name = '可选闭眼短眼裂-随源开度非自主眨眼'; mesh.visible = false; parent.add(mesh);
  return { apply(openness, enabled) {
    mesh.visible = enabled && openness < .62;
    if (!mesh.visible) return { visible: false, drivenOpenness: openness };
    const radius = .006 * (1 - openness / .62), positions = geometry.attributes.position.array;
    for (let i = 0; i <= U; i++) {
      const u = -1 + 2 * i / U, arc = Math.sqrt(Math.max(0, 1 - u * u)), x = u * .165;
      const y = arc * (.115 * openness - .008 * (1 - openness));
      const z = .15 * Math.sqrt(Math.max(0, 1 - (x / .19) ** 2 - (y / .175) ** 2)) + .016;
      for (let q = 0; q < 4; q++) {
        const angle = Math.PI * q / 2;
        positions.set([x, y + radius * Math.cos(angle), z + radius * Math.sin(angle)], (i * 4 + q) * 3);
      }
    }
    geometry.attributes.position.needsUpdate = true; geometry.computeVertexNormals(); geometry.computeBoundingSphere();
    return { visible: true, drivenOpenness: openness, radiusLocal: radius, centerYLocal: .115 * openness - .008 * (1 - openness), sourceTimingChanged: false };
  } };
}

// Optional canine oral display only. This creates no source measurement and
// never changes the skull, upper muzzle, nose, eyeballs, head or body scale.
export function createMouthDisplay(head, { fur, dark, oral, tooth }) {
  const root = new T.Group(); root.name = '可选犬类局部口形-工程显示非测量'; root.visible = false; head.add(root);
  const radialBands = 12;
  const skinGeometry = new T.BufferGeometry();
  skinGeometry.setAttribute('position', new T.BufferAttribute(new Float32Array(N * (radialBands + 1) * 3), 3));
  const skinIndices = [];
  for (let i = 0; i < N; i++) for (let band = 0; band < radialBands; band++) {
    const a = i * (radialBands + 1) + band, b = ((i + 1) % N) * (radialBands + 1) + band;
    skinIndices.push(a, a + 1, b, b, a + 1, b + 1);
  }
  skinGeometry.setIndex(skinIndices);
  const skin = new T.Mesh(skinGeometry, fur); skin.name = '犬口鼻连续毛皮面-真实内孔无中心填面'; root.add(skin);
  const backGeometry = new T.BufferGeometry();
  backGeometry.setAttribute('position', new T.BufferAttribute(new Float32Array((N + 1) * 3), 3));
  const backIndices = [];
  for (let i = 0; i < N; i++) backIndices.push(0, 1 + (i + 1) % N, 1 + i);
  backGeometry.setIndex(backIndices);
  const back = new T.Mesh(backGeometry, fur); back.name = '犬口鼻背侧壳-位于暗腔之后'; root.add(back);
  const cavityGeometry = new T.BufferGeometry();
  cavityGeometry.setAttribute('position', new T.BufferAttribute(new Float32Array((N + 1) * 3), 3));
  const cavityIndices = [];
  for (let i = 0; i < N; i++) cavityIndices.push(0, 1 + i, 1 + (i + 1) % N);
  cavityGeometry.setIndex(cavityIndices);
  const pocket = new T.Mesh(cavityGeometry, oral); pocket.name = '真实局部凹口腔轮廓'; root.add(pocket);

  function contourMesh(name, mat) {
    const geometry = new T.BufferGeometry();
    geometry.setAttribute('position', new T.BufferAttribute(new Float32Array(N * 4 * 3), 3));
    const indices = [];
    for (let i = 0; i < N; i++) for (let q = 0; q < 4; q++) {
      const a = i * 4 + q, b = ((i + 1) % N) * 4 + q;
      const c = ((i + 1) % N) * 4 + (q + 1) % 4, d = i * 4 + (q + 1) % 4;
      indices.push(a, b, d, b, c, d);
    }
    geometry.setIndex(indices);
    const mesh = new T.Mesh(geometry, mat); mesh.name = name; root.add(mesh);
    return mesh;
  }
  const innerRim = contourMesh('薄犬口缘', dark);
  const seamU = 56, seamV = 8, seamGeometry = new T.BufferGeometry(), seamIndices = [];
  seamGeometry.setAttribute('position', new T.BufferAttribute(new Float32Array((seamU + 1) * seamV * 3), 3));
  for (let i = 0; i < seamU; i++) for (let j = 0; j < seamV; j++) {
    const a = i * seamV + j, b = (i + 1) * seamV + j, c = (i + 1) * seamV + (j + 1) % seamV, d = i * seamV + (j + 1) % seamV;
    // u advances along the lip while theta follows (normal, tangent×normal).
    // The previous a,b,d winding faced into the tube. Front-side culling
    // exposed only its buried back half, erasing the closed lip in renders.
    seamIndices.push(a, d, b, b, d, c);
  }
  seamGeometry.setIndex(seamIndices);
  const closedSeam = new T.Mesh(seamGeometry, dark); closedSeam.name = 'v12-surface-seated-short-corner-seam'; closedSeam.visible = false; root.add(closedSeam);
  const toothGeometry = new T.ConeGeometry(1, 1, 5);
  const teeth = [-.68, -.27, -.09, .09, .27, .68].map(q => {
    const mesh = new T.Mesh(toothGeometry, tooth); mesh.name = '低调上犬齿缘-闭口隐藏'; mesh.rotation.z = Math.PI; root.add(mesh); return { q, mesh };
  });
  let report = { enabled: false, engineeringDisplayOnly: true, sourceMeasurement: false };

  function apply(control, rawFace = {}, faceDesign = null) {
    const designEnabled = faceDesign?.enabled === true;
    const mouthDesign = designEnabled ? faceDesign.mouth || {} : {};
    closedSeam.visible = false; innerRim.visible = true; pocket.visible = true;
    const enabled = control?.enabled === true; root.visible = enabled;
    if (!enabled) { report = { enabled: false, engineeringDisplayOnly: true, sourceMeasurement: false }; return report; }
    const requestedJaw = clamp(control.jawDisplay, 0, 1, clamp(rawFace.mouth, 0, 1, 0));
    const widthControl = clamp(control.mouthWidth, .35, 1.15, 1);
    const round = clamp(control.lipRound, 0, 1, 0), press = clamp(control.lipPress, 0, 1, 0);
    const left = clamp(control.cornerLeft, -1, 1, 0), right = clamp(control.cornerRight, -1, 1, 0);
    const toothControl = clamp(control.upperTeeth, 0, 1, 0);
    // Press closes the aperture and jaw, rather than keeping an open dark slit
    // under a pressed lip. Roundness changes the entire oral contour width.
    const jawDisplay = requestedJaw * (1 - .96 * press);
    const halfWidth = .29 * widthControl * (1 - (designEnabled ? .30 : .55) * round) * (1 - .10 * press);
    // The opening is seated between the fixed upper muzzle and the real
    // rotated lower-jaw surface. It is not an oval hovering in front of both.
    const jawAngle = jawDisplay * .75, topY = -.305;
    const jawScaleX = .92 + .08 * widthControl - .08 * round;
    const lowerJawAnchor = [0, -.12 - .17 * Math.cos(jawAngle) - .49 * Math.sin(jawAngle), .22 - .17 * Math.sin(jawAngle) + .49 * Math.cos(jawAngle)];
    const bottomY = Math.min(topY - .002 * (1 - .75 * press), lowerJawAnchor[1]);
    const centerY = (topY + bottomY) / 2, halfHeight = (topY - bottomY) / 2;
    const aperture = halfHeight * 2;
    const seamRequested = designEnabled && mouthDesign.closedSeam === true;
    const closedWeight = seamRequested ? 1 - smooth(.006, .018, aperture) : 0;
    // Wide and round openings use the same support field. The former split
    // upper/chin surfaces caused the white plate and sharp commissure folds.
    const openEnvelopeWeight = designEnabled && closedWeight < 1 ? 1 : 0;
    const turnStart = clamp(mouthDesign.cornerTurnStart, .30, .90, .55);
    const turnLeft = clamp(mouthDesign.cornerTurnLeft, -.05, .07, 0), turnRight = clamp(mouthDesign.cornerTurnRight, -.05, .07, 0);
    const seamRadius = clamp(mouthDesign.seamRadius, .0045, .006, .0055);
    const seamOffset = clamp(mouthDesign.seamNormalOffset, .001, .006, .0035);
    const seamY = u => centerY + (u >= 0 ? turnLeft : turnRight) * smooth(turnStart, 1, Math.abs(u));
    const upperSurfaceZ = (x, y) => .48 + .32 * Math.sqrt(Math.max(0, 1 - (x / .37) ** 2 - ((y + .20) / .19) ** 2));
    const topZ = upperSurfaceZ(0, topY) + .0015;
    const lowerZ = lowerJawAnchor[2] + .0015;
    const cosJaw = Math.cos(jawAngle), sinJaw = Math.sin(jawAngle);
    const outerBottom = [0, -.12 - .37 * cosJaw - .30 * sinJaw, .22 - .37 * sinJaw + .30 * cosJaw];
    const closedTopY = -.01, closedCenterY = (closedTopY + outerBottom[1]) / 2;
    const closedRadiusY = (closedTopY - outerBottom[1]) / 2, closedRadiusX = .37, closedCenterZ = .48;
    const closedAnchorY = seamY(0);
    const closedAnchorArc = Math.sqrt(Math.max(1e-12, 1 - ((closedAnchorY - closedCenterY) / closedRadiusY) ** 2));
    const closedRadiusZ = clamp((upperSurfaceZ(0, closedAnchorY) - closedCenterZ) / closedAnchorArc, .26, .32, .273);
    const closedField = (x, y) => closedCenterZ + closedRadiusZ * Math.sqrt(Math.max(0, 1 - (x / closedRadiusX) ** 2 - ((y - closedCenterY) / closedRadiusY) ** 2));
    const closedNormal = (x, y, z) => new T.Vector3(x / closedRadiusX ** 2, (y - closedCenterY) / closedRadiusY ** 2, (z - closedCenterZ) / closedRadiusZ ** 2).normalize();
    const blendPoint = (open, closed) => open.map((n, i) => n + (closed[i] - n) * closedWeight);
    const openOutlinePoint = (u, t) => {
      const corner = u >= 0 ? left : right, x = halfWidth * u;
      if (designEnabled) {
        // Broad, softly lifted corners join a single convex muzzle field.
        // Neither lip edge is a separate bright slab or an inset oval sticker.
        const middleY = seamY(u), y = middleY + halfHeight * t;
        return [x, y, closedField(x, y)];
      }
      // Narrow smiles lift connected commissures; the upper/lower edges meet
      // there rather than retaining a neutral ellipse with a displaced center.
      const baseCornerY = (-.313) * (1 - round) + centerY * round;
      const cornerY = baseCornerY + .095 * corner * (1 - .25 * round);
      const y = t >= 0 ? cornerY + (topY - cornerY) * t : cornerY + (bottomY - cornerY) * -t;
      const chinZ = lowerZ - .045 * Math.abs(u) ** 1.5;
      const upperZ = upperSurfaceZ(x, topY) + .0015;
      const muzzleCornerZ = upperSurfaceZ(x, Math.max(-.332, Math.min(-.245, cornerY))) + .0015;
      const roundCornerZ = (upperZ + chinZ) / 2;
      const cornerZ = muzzleCornerZ * (1 - round) + roundCornerZ * round;
      const z = t >= 0 ? cornerZ + (upperZ - cornerZ) * t : cornerZ + (chinZ - cornerZ) * -t;
      return [x, y, z];
    };
    const outlinePoint = (u, t) => {
      const original = openOutlinePoint(u, t);
      const open = openEnvelopeWeight > 0 ? [original[0], original[1], original[2] + (closedField(original[0], original[1]) - original[2]) * openEnvelopeWeight] : original;
      if (closedWeight <= 0) return open;
      const x = halfWidth * u, y = seamY(u);
      const blended = blendPoint(open, [x, y, closedField(x, y)]);
      if (designEnabled) blended[2] = closedField(blended[0], blended[1]);
      return blended;
    };
    const points = [], openPoints = [];
    for (let i = 0; i < N; i++) {
      const angle = 2 * Math.PI * i / N, u = Math.cos(angle), t = Math.sin(angle);
      points.push(outlinePoint(u, t));
      openPoints.push(openOutlinePoint(u, t));
    }
    // Replace the closed legacy snout/chin balls with an actual annular skin.
    // The opening has only boundary vertices; NO fur triangles span its hole.
    // Both surface references retain the original ellipsoid dimensions.
    const chinCenterY = -.12 - .24 * cosJaw - .30 * sinJaw;
    const chinCenterZ = .22 - .24 * sinJaw + .30 * cosJaw;
    const chinSurfaceZ = (x, y) => {
      const dy = y - chinCenterY, iy = 1 / (.13 * .13), iz = 1 / (.25 * .25);
      const a = sinJaw * sinJaw * iy + cosJaw * cosJaw * iz;
      const b = 2 * cosJaw * sinJaw * dy * (iy - iz);
      const c = dy * dy * (cosJaw * cosJaw * iy + sinJaw * sinJaw * iz) + (x / (.31 * jawScaleX)) ** 2 - 1;
      const disc = b * b - 4 * a * c;
      return disc < 0 ? null : chinCenterZ + (-b + Math.sqrt(disc)) / (2 * a);
    };
    const skinPositions = skinGeometry.attributes.position.array, backPositions = backGeometry.attributes.position.array;
    backPositions.set([0, (-.01 + outerBottom[1]) / 2, .235]);
    const outerPoints = [];
    for (let i = 0; i < N; i++) {
      const angle = 2 * Math.PI * i / N, u = Math.cos(angle), t = Math.sin(angle), inner = openPoints[i];
      const exteriorWidth = .37 * (1 - Math.max(0, -t)) + .31 * jawScaleX * Math.max(0, -t);
      const outer = [exteriorWidth * u, t >= 0 ? -.20 + .19 * t : -.20 + (outerBottom[1] + .20) * -t, t >= 0 ? .48 : .48 + (outerBottom[2] - .48) * -t];
      const closedOuter = [closedRadiusX * u, closedCenterY + closedRadiusY * t, closedCenterZ];
      const supportedOuter = openEnvelopeWeight > 0 ? outer.map((n, j) => n + (closedOuter[j] - n) * openEnvelopeWeight) : outer;
      const finalOuter = closedWeight > 0 ? blendPoint(supportedOuter, closedOuter) : supportedOuter;
      outerPoints.push(finalOuter); backPositions.set(finalOuter, (i + 1) * 3);
      for (let band = 0; band <= radialBands; band++) {
        const radial = band / radialBands, x = inner[0] + (outer[0] - inner[0]) * radial, y = inner[1] + (outer[1] - inner[1]) * radial;
        const linearZ = inner[2] + (outer[2] - inner[2]) * radial;
        const inUpperEllipse = 1 - (x / .37) ** 2 - ((y + .20) / .19) ** 2;
        const referenceZ = t >= 0 ? inUpperEllipse >= 0 ? upperSurfaceZ(x, y) : null : chinSurfaceZ(x, y);
        const z = referenceZ == null ? linearZ : linearZ + (referenceZ - linearZ) * Math.sin(Math.PI * radial);
        let vertex = [x, y, z];
        if (openEnvelopeWeight > 0) {
          // One smooth support field around an actual open inner ring. The
          // aperture keeps its x/y and topology; no fur fills the mouth hole.
          const sx = inner[0] + (closedOuter[0] - inner[0]) * radial;
          const sy = inner[1] + (closedOuter[1] - inner[1]) * radial;
          const supported = [sx, sy, closedField(sx, sy)];
          vertex = vertex.map((n, j) => n + (supported[j] - n) * openEnvelopeWeight);
        }
        if (closedWeight > 0) {
          const closedInner = [halfWidth * u, seamY(u)];
          const cx = closedInner[0] + (closedOuter[0] - closedInner[0]) * radial;
          const cy = closedInner[1] + (closedOuter[1] - closedInner[1]) * radial;
          vertex = blendPoint(vertex, [cx, cy, closedField(cx, cy)]);
        }
        if (designEnabled) vertex[2] = closedField(vertex[0], vertex[1]);
        skinPositions.set(vertex, (i * (radialBands + 1) + band) * 3);
      }
    }
    for (const geometry of [skinGeometry, backGeometry]) {
      geometry.attributes.position.needsUpdate = true; geometry.computeVertexNormals(); geometry.computeBoundingSphere();
    }
    if (designEnabled || closedWeight === 1 || (openEnvelopeWeight === 1 && closedWeight === 0)) {
      // Both sides of the coincident contact line use the same analytic
      // ellipsoid normal; no upper/chin branch can leave a shading ridge.
      const normals = skinGeometry.attributes.normal.array;
      for (let k = 0; k < skinPositions.length; k += 3) normals.set(closedNormal(skinPositions[k], skinPositions[k + 1], skinPositions[k + 2]).toArray(), k);
      skinGeometry.attributes.normal.needsUpdate = true;
    }
    const array = cavityGeometry.attributes.position.array;
    const centerZ = (topZ + lowerZ) / 2 - .028;
    array.set([0, centerY, centerZ]);
    points.forEach((p, i) => array.set(p, (i + 1) * 3));
    cavityGeometry.attributes.position.needsUpdate = true; cavityGeometry.computeVertexNormals(); cavityGeometry.computeBoundingSphere();

    function setRim(mesh, radius, outward) {
      const positions = mesh.geometry.attributes.position.array;
      for (let i = 0; i < N; i++) {
        const angle = 2 * Math.PI * i / N, u = Math.cos(angle), t = Math.sin(angle), p = points[i];
        // Local 4-sided tube; stable topology, no whole-face morph or raster.
        for (let q = 0; q < 4; q++) {
          const a = q * Math.PI / 2;
          positions.set([p[0] + u * (outward + radius * Math.cos(a)), p[1] + t * (outward + radius * Math.cos(a)), p[2] + radius * Math.sin(a)], (i * 4 + q) * 3);
        }
      }
      mesh.geometry.attributes.position.needsUpdate = true; mesh.geometry.computeVertexNormals(); mesh.geometry.computeBoundingSphere();
    }
    // No complete white fur tube: it read as a detached bright sticker in
    // actual frame review. The fixed muzzle/chin now occlude the recessed rim.
    setRim(innerRim, (.0027 + .0008 * press) * (1 - closedWeight), 0);
    innerRim.visible = closedWeight < .999;
    const seamSamples = [];
    closedSeam.visible = closedWeight > .001;
    pocket.visible = !designEnabled || closedWeight < .999;
    if (closedSeam.visible) {
      const seamPositions = seamGeometry.attributes.position.array;
      const point = u => new T.Vector3(halfWidth * u, seamY(u), closedField(halfWidth * u, seamY(u)) + seamOffset);
      for (let i = 0; i <= seamU; i++) {
        const u = i / seamU * 2 - 1, p = point(u);
        const tangent = point(Math.min(1, u + .001)).sub(point(Math.max(-1, u - .001))).normalize();
        const normal = closedNormal(p.x, p.y, closedField(p.x, p.y));
        const across = new T.Vector3().crossVectors(tangent, normal).normalize();
        const radius = seamRadius * closedWeight * (.20 + .80 * smooth(0, .16, 1 - Math.abs(u)));
        seamSamples.push({ u, position: p.toArray(), radius });
        for (let j = 0; j < seamV; j++) {
          const angle = j / seamV * Math.PI * 2;
          const vertex = p.clone().addScaledVector(normal, radius * Math.cos(angle)).addScaledVector(across, radius * Math.sin(angle));
          seamPositions.set(vertex.toArray(), (i * seamV + j) * 3);
        }
      }
      seamGeometry.attributes.position.needsUpdate = true; seamGeometry.computeVertexNormals(); seamGeometry.computeBoundingSphere();
    }
    const showTeeth = toothControl > 0 && aperture > .020 && press < .5;
    const visibleTeeth = [];
    for (const { q, mesh } of teeth) {
      mesh.visible = showTeeth;
      const length = (Math.abs(q) > .5 ? .025 : .015) * toothControl * Math.min(1, aperture / .14);
      const upper = outlinePoint(q, Math.sqrt(1 - q * q)), x = halfWidth * q, top = upper[1];
      const width = Math.min(.008, halfWidth * .08);
      mesh.scale.set(width, Math.max(.0001, length), width * .65);
      mesh.position.set(x, top - length / 2 - .002, upper[2] + .003);
      if (showTeeth) visibleTeeth.push({ position: mesh.position.toArray(), length, width: width * 2 });
    }
    const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
    let closedFieldResidualMaxLocal = null, closedContactGapMaxLocal = null, closedContactNormalDifferenceMax = null;
    if (closedWeight === 1) {
      closedFieldResidualMaxLocal = 0; closedContactGapMaxLocal = 0; closedContactNormalDifferenceMax = 0;
      const normals = skinGeometry.attributes.normal.array;
      for (let k = 0; k < skinPositions.length; k += 3) closedFieldResidualMaxLocal = Math.max(closedFieldResidualMaxLocal, Math.abs(skinPositions[k + 2] - closedField(skinPositions[k], skinPositions[k + 1])));
      for (let i = 0; i < N; i++) {
        const a = i * (radialBands + 1) * 3, b = ((N - i) % N) * (radialBands + 1) * 3;
        closedContactGapMaxLocal = Math.max(closedContactGapMaxLocal, Math.hypot(skinPositions[a] - skinPositions[b], skinPositions[a + 1] - skinPositions[b + 1], skinPositions[a + 2] - skinPositions[b + 2]));
        closedContactNormalDifferenceMax = Math.max(closedContactNormalDifferenceMax, Math.hypot(normals[a] - normals[b], normals[a + 1] - normals[b + 1], normals[a + 2] - normals[b + 2]));
      }
    }
    report = {
      enabled: true, engineeringDisplayOnly: true, sourceMeasurement: false,
      requested: { jawDisplay: requestedJaw, mouthWidth: widthControl, lipRound: round, lipPress: press, upperTeeth: toothControl, cornerLeft: left, cornerRight: right },
      actual: { jawDisplay, jawRotationRadians: jawAngle, contourWidthLocal: Math.max(...xs) - Math.min(...xs), contourHeightLocal: Math.max(...ys) - Math.min(...ys), centerApertureHeightLocal: aperture * (1 - closedWeight), requestedCenterApertureHeightLocal: aperture, openingCenterLocal: [0, centerY, centerZ], cornerLeftLocal: points[0], cornerRightLocal: points[N / 2], upperCenterAnchorLocal: [0, topY, topZ], lowerJawSurfaceAnchorLocal: lowerJawAnchor, brightOuterFurTube: false, darkRimRadiusLocal: (.0027 + .0008 * press) * (1 - closedWeight), visibleUpperTeeth: visibleTeeth,
        mouthSkin: { type: 'annular_fur_surface_with_true_aperture', legacyClosedUpperAndLowerBallsMustBeHidden: true, innerBoundaryVertices: N, radialBands, furTrianglesAcrossAperture: 0, upperEnvelope: { center: [0, -.20, .48], radii: [.37, .19, .32] }, lowerEnvelope: { pivot: [0, -.12, .22], centerInJaw: [0, -.24, .30], radii: [.31 * jawScaleX, .13, .25] }, outerTopAnchor: outerPoints[N / 4], outerBottomAnchor: outerPoints[3 * N / 4], backShellBehindCavity: true }, noseAndEyeballsUnchanged: true },
      faceDesign: { enabled: designEnabled, sourceMeasurement: false, closedSeamRequested: seamRequested, closedSeamVisible: closedSeam.visible, closedWeight, openEnvelopeWeight, turnStart, cornerTurnLeft: turnLeft, cornerTurnRight: turnRight, seamRadiusLocal: seamRadius, seamNormalOffsetLocal: seamOffset, seamSamples, centralSeamFlatThroughAbsU: turnStart, surface: designEnabled ? 'continuous_open_closed_muzzle_geometry' : 'existing_upper_muzzle_ellipsoid',
        closedEnvelope: { enabled: closedWeight > 0, center: [0, closedCenterY, closedCenterZ], radii: [closedRadiusX, closedRadiusY, closedRadiusZ], topY: closedTopY, bottomY: outerBottom[1], anchorY: closedAnchorY, anchorZ: upperSurfaceZ(0, closedAnchorY), skinNormalOffsetLocal: 0, seamOnlyDepthOffsetLocal: seamOffset },
        openEnvelope: { weight: openEnvelopeWeight, driver: 'all_authored_openings_share_single_convex_muzzle_field', preservesOriginalApertureXY: true, fillsMouthHole: false, supportsAllAnglesAndRadialBandsWithSingleField: openEnvelopeWeight === 1, analyticNormalsWhenFullySupported: openEnvelopeWeight === 1 && closedWeight === 0, sourceTimingChanged: false },
        continuity: { closedSurfaceUsesSingleField: true, closedSurfaceUpperChinBranching: false, fixedSkinTopology: true, transition: 'per_vertex_open_closed_geometry_mix', analyticNormalsWhenFullyClosed: closedWeight === 1, closedFieldResidualMaxLocal, closedContactGapMaxLocal, closedContactNormalDifferenceMax, sourceClockChanged: false }, visualAcceptance: false },
      formulas: { jaw: 'jawDisplay=requestedJaw*(1-.96*lipPress); jawX=jawDisplay*.75 rad; engineering canine display, NOT measured canine jaw angle', halfWidth: designEnabled ? '.29*mouthWidth*(1-.30*lipRound)*(1-.10*lipPress)' : '.29*mouthWidth*(1-.55*lipRound)*(1-.10*lipPress)', aperture: 'top y=-.305; bottom follows rotated existing lower-jaw point (0,-.17,.49), clamped closed', depth: 'authored open/closed annulus shares one convex muzzle field and analytic normals; dark cavity remains a true aperture', corners: designEnabled ? 'centerY+cornerTurn*smoothstep(cornerTurnStart,1,abs(u)); softly distributed corner lift; closed upper/lower boundaries share seam' : 'commissureY=mix(-.313,centerY,lipRound)+.095*corner*(1-.25*lipRound); upper/lower skin edges share exact corner', teeth: 'only if aperture>.020 && press<.5; .015/.025 local-unit upper tooth edge, shortened continuously with gap' },
    };
    return structuredClone(report);
  }
  return { root, apply, diagnostics: () => structuredClone(report) };
}
