export const FACE_GEOMETRY_BUILD_ID = 'c4715de1cc26a93124c2abd07cbda9c90f4014b85d120442e31b75c11de30c10';
import * as T from './vendor/three.module.js';

const clamp = (n, lo, hi, fallback = 0) => Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : fallback;
const smooth = (a, b, n) => { const t = clamp((n - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
const radii = [.17, .135, .12];
const lidOffset = .0027, irisOffset = .0015, pupilOffset = .0021;

function finish(geometry) {
  geometry.attributes.position.needsUpdate = true;
  geometry.computeVertexNormals(); geometry.computeBoundingSphere();
}
function surface(x, y, offset = lidOffset) {
  const z = radii[2] * Math.sqrt(Math.max(0, 1 - (x / radii[0]) ** 2 - (y / radii[1]) ** 2));
  const n = new T.Vector3(x / radii[0] ** 2, y / radii[1] ** 2, z / radii[2] ** 2).normalize();
  return new T.Vector3(x, y, z).addScaledVector(n, offset);
}
function gridMesh(parent, name, U, V, mat) {
  const geometry = new T.BufferGeometry(), indices = [];
  geometry.setAttribute('position', new T.BufferAttribute(new Float32Array((U + 1) * (V + 1) * 3), 3));
  for (let i = 0; i < U; i++) for (let j = 0; j < V; j++) {
    const a = i * (V + 1) + j, b = a + V + 1;
    indices.push(a, b, a + 1, b, b + 1, a + 1);
  }
  geometry.setIndex(indices);
  const m = new T.Mesh(geometry, mat.clone()); m.material.side = T.DoubleSide; m.name = name; parent.add(m);
  return m;
}
function capMesh(parent, name, mat) {
  const rings = 9, slices = 48, geometry = new T.BufferGeometry(), indices = [];
  geometry.setAttribute('position', new T.BufferAttribute(new Float32Array((1 + rings * slices) * 3), 3));
  for (let k = 0; k < slices; k++) indices.push(0, 1 + k, 1 + (k + 1) % slices);
  for (let j = 0; j < rings - 1; j++) for (let k = 0; k < slices; k++) {
    const a = 1 + j * slices + k, b = 1 + j * slices + (k + 1) % slices, c = a + slices, d = b + slices;
    indices.push(a, c, b, b, c, d);
  }
  geometry.setIndex(indices);
  const m = new T.Mesh(geometry, mat); m.name = name; parent.add(m);
  return { mesh: m, rings, slices };
}
function setCap(cap, sx, sy, rotation, offset, shift = [0, 0]) {
  const positions = cap.mesh.geometry.attributes.position.array;
  function point(x, y) {
    const q = new T.Vector3(x, y, Math.sqrt(Math.max(0, 1 - x * x - y * y))).applyQuaternion(rotation);
    const p = new T.Vector3(q.x * radii[0], q.y * radii[1], q.z * radii[2]);
    const normal = new T.Vector3(q.x / radii[0], q.y / radii[1], q.z / radii[2]).normalize();
    return p.addScaledVector(normal, offset).toArray();
  }
  positions.set(point(shift[0], shift[1]), 0);
  for (let j = 1; j <= cap.rings; j++) for (let k = 0; k < cap.slices; k++) {
    const a = k / cap.slices * Math.PI * 2;
    positions.set(point(shift[0] + sx * j / cap.rings * Math.cos(a), shift[1] + sy * j / cap.rings * Math.sin(a)), (1 + (j - 1) * cap.slices + k) * 3);
  }
  finish(cap.mesh.geometry);
}

// Local authored geometry only. The old eyes remain intact and can be restored
// without changing their transforms, material, source clock or eye centers.
export function createFaceEyeGeometry(eye, head, side, { fur, coat, iris, pupil }) {
  const root = new T.Group(); root.name = 'v12-surface-seated-eye'; root.visible = false; eye.add(root);
  const U = 48, V = 20;
  const upper = gridMesh(root, 'v12-upper-lid-on-fixed-eye', U, V, fur);
  const lower = gridMesh(root, 'v12-lower-lid-on-fixed-eye', U, V, fur);
  // The lid is facial skin, rather than another white eyeball. A continuous
  // coat/fur transition seats the upper lid in the brow and the lower in cheek.
  // The fixed globe and its transform remain untouched.
  for (const [isUpper, mesh] of [[true, upper], [false, lower]]) {
    const colors = new Float32Array((U + 1) * (V + 1) * 3);
    mesh.material.color.set('#ffffff'); mesh.material.vertexColors = true;
    mesh.material.roughness = .96;
    for (let i = 0; i <= U; i++) for (let j = 0; j <= V; j++) {
      const t = j / V, outer = Math.abs(i / U * 2 - 1);
      const coatWeight = isUpper ? .46 + .46 * smooth(0, .85, t) : .09 + .29 * smooth(.2, 1, t) + .10 * outer;
      const color = fur.color.clone().lerp(coat.color, coatWeight);
      // A soft shaded fold belongs to the fur; there is no bright rim tube.
      color.multiplyScalar(1 - .11 * (1 - smooth(0, .20, t)));
      colors.set(color.toArray(), (i * (V + 1) + j) * 3);
    }
    mesh.geometry.setAttribute('color', new T.BufferAttribute(colors, 3));
  }
  const waterlineV = 8;
  const upperWaterline = gridMesh(root, 'v12-upper-surface-waterline', U, waterlineV, pupil);
  const lowerWaterline = gridMesh(root, 'v12-lower-surface-waterline', U, waterlineV, pupil);
  upperWaterline.visible = false; lowerWaterline.visible = false;
  const irisCap = capMesh(root, 'v12-curved-iris', iris);
  const pupilCap = capMesh(root, 'v12-curved-pupil', pupil);
  const glint = capMesh(root, 'v12-fixed-light-catch', new T.MeshStandardMaterial({ color: '#fff9ec', roughness: .28 }));
  const browRoot = new T.Group(); browRoot.name = 'v12-short-curved-brow'; browRoot.visible = false; head.add(browRoot);
  const browU = 24, browV = 8, brow = gridMesh(browRoot, 'v12-connected-tapered-brow', browU, browV, coat);
  let report = { enabled: false, sourceMeasurement: false, visualAcceptance: false };

  function apply(enabled, openness, control = {}, browControl = {}, gaze = {}, endpoints) {
    root.visible = enabled; browRoot.visible = enabled;
    upperWaterline.visible = enabled; lowerWaterline.visible = enabled && openness > .015;
    if (!enabled) return report = { enabled: false, sourceMeasurement: false, visualAcceptance: false };
    const upperInnerDrop = clamp(control.upperInnerDrop, -.05, .07), upperOuterDrop = clamp(control.upperOuterDrop, -.05, .07);
    const lowerOuterLift = clamp(control.lowerOuterLift, -.03, .05), outerPinch = clamp(control.outerPinch, 0, .04);
    const activate = smooth(0, .18, openness);
    const lowerBase = (.045 + .030 * openness) * smooth(0, .22, openness);
    const innerEdges = { upper: [], lower: [] };
    for (const [isUpper, mesh] of [[true, upper], [false, lower]]) {
      const positions = mesh.geometry.attributes.position.array;
      for (let i = 0; i <= U; i++) {
        const u = i / U * 2 - 1, arc = Math.sqrt(Math.max(0, 1 - u * u));
        const outerFraction = (u * side + 1) / 2, outerWeight = smooth(.40, 1, outerFraction);
        const drop = upperInnerDrop + (upperOuterDrop - upperInnerDrop) * outerFraction;
        // Calibrate lid coverage for the surface-seated iris. This changes
        // neither the fixed white-eye size nor the source openness/blink clock.
        const closedFoldY = -.007 * arc * (1 - activate);
        const upperY = closedFoldY + arc * Math.max(0, .090 * openness - activate * (drop + outerPinch * outerWeight));
        const lowerY = closedFoldY + arc * Math.min(0, -lowerBase + activate * (lowerOuterLift + .35 * outerPinch) * outerWeight);
        const innerY = isUpper ? upperY : Math.min(upperY, lowerY), innerX = u * radii[0];
        const inner = surface(innerX, innerY);
        innerEdges[isUpper ? 'upper' : 'lower'].push(inner.toArray());
        for (let j = 0; j <= V; j++) {
          const t = j / V, x = innerX + (u * .202 - innerX) * t;
          const y = innerY + ((isUpper ? 1 : -1) * (.165 * arc + .008) - innerY) * t;
          const q = (x / radii[0]) ** 2 + (y / radii[1]) ** 2;
          // Only the inner eye-facing band follows the eye; the outer band
          // continues into the existing fur rather than hovering in front.
          const p = q <= 1 ? surface(x, y) : new T.Vector3(x, y, -.010 * smooth(1, 1.45, q));
          positions.set(p.toArray(), (i * (V + 1) + j) * 3);
        }
      }
      finish(mesh.geometry);
    }
    // These are small 3D tubes seated on the same moving lid edge, with only
    // .0004 normal separation from the fur surface. The lower tube narrows
    // into the upper eye slit as the driven source openness approaches zero.
    const lowerLineWeight = smooth(.015, .18, openness);
    const waterlineNormalAdvance = .0004;
    for (const [isUpper, mesh] of [[true, upperWaterline], [false, lowerWaterline]]) {
      const edge = innerEdges[isUpper ? 'upper' : 'lower'];
      const positions = mesh.geometry.attributes.position.array;
      const baseRadius = isUpper ? .0025 : .00135 * lowerLineWeight;
      for (let i = 0; i <= U; i++) {
        const p = new T.Vector3(...edge[i]);
        const tangent = new T.Vector3(...edge[Math.min(U, i + 1)]).sub(new T.Vector3(...edge[Math.max(0, i - 1)])).normalize();
        const normal = new T.Vector3(p.x / radii[0] ** 2, p.y / radii[1] ** 2, p.z / radii[2] ** 2).normalize();
        const across = new T.Vector3().crossVectors(tangent, normal).normalize();
        const u = i / U * 2 - 1, radius = baseRadius * (.18 + .82 * smooth(0, .20, 1 - Math.abs(u)));
        p.addScaledVector(normal, waterlineNormalAdvance);
        for (let j = 0; j <= waterlineV; j++) {
          const angle = j / waterlineV * Math.PI * 2;
          const vertex = p.clone().addScaledVector(across, radius * Math.cos(angle)).addScaledVector(normal, radius * .45 * Math.sin(angle));
          positions.set(vertex.toArray(), (i * (waterlineV + 1) + j) * 3);
        }
      }
      finish(mesh.geometry);
    }
    const maxYaw = 22 * Math.PI / 180, maxPitch = 12 * Math.PI / 180;
    const requestedYaw = Number.isFinite(gaze.yaw) ? gaze.yaw : clamp(gaze.x, -1, 1) * maxYaw;
    const requestedPitch = Number.isFinite(gaze.pitch) ? gaze.pitch : clamp(gaze.y, -1, 1) * maxPitch;
    const yaw = clamp(requestedYaw, -maxYaw, maxYaw), pitch = clamp(requestedPitch, -maxPitch, maxPitch);
    const rotation = new T.Quaternion().setFromEuler(new T.Euler(-pitch, yaw, 0, 'YXZ'));
    setCap(irisCap, .069 / .17, .084 / .135, rotation, irisOffset);
    setCap(pupilCap, .069 * .49 / .17, .084 * .65 / .135, rotation, pupilOffset);
    setCap(glint, .020, .025, rotation, .0028, [-.090, .16]);

    const innerDelta = clamp(browControl.innerDelta, -.05, .07), outerDelta = clamp(browControl.outerDelta, -.05, .07), curve = clamp(browControl.curve, -.02, .05, .012);
    const start = new T.Vector3(...endpoints.inner), end = new T.Vector3(...endpoints.outer);
    const browPositions = brow.geometry.attributes.position.array, centerline = [];
    for (let i = 0; i <= browU; i++) {
      const t = i / browU, along = .10 + .80 * t;
      const p = start.clone().lerp(end, along);
      p.y += innerDelta * (1 - along) + outerDelta * along + Math.sin(Math.PI * t) * curve;
      centerline.push(p.toArray());
      const radius = .003 + .015 * Math.sin(Math.PI * t) ** .7;
      for (let j = 0; j <= browV; j++) {
        const angle = j / browV * Math.PI * 2;
        browPositions.set([p.x, p.y + radius * Math.cos(angle), p.z + radius * .70 * Math.sin(angle)], (i * (browV + 1) + j) * 3);
      }
    }
    finish(brow.geometry);
    report = { enabled: true, sourceMeasurement: false, visualAcceptance: false,
      fixedEyeCenter: eye.position.toArray(), fixedEyeRadii: [...radii],
      lidSurface: 'fixed_eye_ellipsoid_with_continuous_coat_fur_fold_and_rear_seated_outer_band', lidNormalOffsetLocal: lidOffset,
      lidColor: { upperCoatBlend: [.46, .92], lowerCoatBlend: [.09, .48], whiteShellMaterial: false, fixedGlobeChanged: false },
      irisSurface: 'rotated_unit_sphere_disc_scaled_to_fixed_eye_ellipsoid', irisNormalOffsetLocal: irisOffset,
      pupilNormalOffsetLocal: pupilOffset, lidAheadOfPupilByLocal: lidOffset - pupilOffset,
      gaze: { requestedYawRadians: requestedYaw, requestedPitchRadians: requestedPitch, yawRadians: yaw, pitchRadians: pitch, clamped: Math.abs(requestedYaw - yaw) > 1e-10 || Math.abs(requestedPitch - pitch) > 1e-10, angularLimitsRadians: [maxYaw, maxPitch], authoredTarget: gaze.targetWorld || null, targetDirectionEyeLocal: gaze.directionLocal || null, targetAlignmentVerified: false, interpretation: 'authored_canine_gaze_estimate_NOT_source_fixation' },
      upperInnerDrop, upperOuterDrop, lowerOuterLift, outerPinch, baseOpenness: openness, baselineUpperCoefficient: .090, fixedIrisVerticalRadius: .084, lowerBaselineLocal: lowerBase,
      upperInnerEdgeLocal: innerEdges.upper, lowerInnerEdgeLocal: innerEdges.lower,
      waterline: { type: 'tapered_3d_tube_on_actual_lid_inner_edge', upperVisible: upperWaterline.visible, lowerVisible: lowerWaterline.visible, upperRadiusLocal: .0025, lowerRadiusLocal: .00135 * lowerLineWeight, normalAdvanceFromLidLocal: waterlineNormalAdvance, normalThicknessRatio: .45, singleClosedEyeSlit: openness <= .015, drivenBySourceOpenness: true, autonomousBlink: false },
      brow: { type: 'short_connected_curved_taper', innerDelta, outerDelta, curve, centerlineLocal: centerline, maximumRadiusLocal: .018 },
      sourceClockChanged: false, autonomousBlink: false, eyeballScaleChanged: false };
    return structuredClone(report);
  }
  return { root, browRoot, apply, diagnostics: () => structuredClone(report) };
}
