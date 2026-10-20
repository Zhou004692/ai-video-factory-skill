export const RIG_BUILD_ID = 'e45060e6908a6565c8d8515fa90c662a6684a8ba89056508d6b167e55e4eef7e';
import * as T from './vendor/three.module.js';
import { createMouthDisplay, createEyelidCrease } from './mouth-shapes.js';
import { createFaceEyeGeometry } from './face-geometry.js';
const sphere = new T.SphereGeometry(1, 16, 10);
const cylinder = new T.CylinderGeometry(1, 1, 1, 10);
const up = new T.Vector3(0, 1, 0);
const materials = new Map();
export const material = (color, roughness = .8) => {
  const key = String(color) + roughness;
  if (!materials.has(key)) materials.set(key, new T.MeshStandardMaterial({ color, roughness }));
  return materials.get(key);
};
export function ball(parent, name, p, size, mat) {
  const m = new T.Mesh(sphere, mat); m.name = name; m.position.set(...p); m.scale.set(...size); parent.add(m); return m;
}
export function segment(parent, name, a, b, radius, mat) {
  const mesh = new T.Mesh(cylinder, mat); mesh.name = name; parent.add(mesh); setSegment(mesh, a, b, radius); return mesh;
}
export function setSegment(mesh, a, b, radius) {
  const from = new T.Vector3(...a), to = new T.Vector3(...b), dir = to.clone().sub(from);
  mesh.position.copy(from.add(to).multiplyScalar(.5));
  mesh.quaternion.setFromUnitVectors(up, dir.clone().normalize());
  mesh.scale.set(radius, Math.max(.001, dir.length()), radius);
}
const v = a => new T.Vector3(...a);
export function torsoTransform(pose = {}) {
  const base = new T.Vector3(pose.pelvisOffsetX || 0, pose.pelvisOffsetY ?? -(pose.knee || 0) * .38, pose.pelvisOffsetZ || 0);
  const quaternion = new T.Quaternion().setFromEuler(new T.Euler(pose.spinePitch || 0, pose.spineYaw || 0, pose.spineRoll || 0));
  const position = base.clone();
  if (pose.hipPivot) { const pivot = v(pose.hipPivot); position.add(pivot).sub(pivot.clone().applyQuaternion(quaternion)); }
  return { base, position, quaternion };
}
function createEyeLid(parent, upper, mat) {
  const geometry = new T.BufferGeometry(), U = 16, V = 3;
  geometry.setAttribute('position', new T.BufferAttribute(new Float32Array((U + 1) * (V + 1) * 3), 3));
  const indices = [];
  for (let i = 0; i < U; i++) for (let j = 0; j < V; j++) {
    const a = i * (V + 1) + j, b = a + V + 1;
    indices.push(a, b, a + 1, b, b + 1, a + 1);
  }
  geometry.setIndex(indices);
  const mesh = new T.Mesh(geometry, mat.clone()); mesh.material.side = T.DoubleSide; parent.add(mesh);
  return { mesh, upper, U, V };
}
function setEyeLid(lid, openness) {
  const positions = lid.mesh.geometry.attributes.position.array;
  for (let i = 0; i <= lid.U; i++) {
    const u = -1 + 2 * i / lid.U, x = u * .185, arc = Math.sqrt(Math.max(0, 1 - u * u));
    const sign = lid.upper ? 1 : -1;
    const innerY = sign * arc * (lid.upper ? .115 : .10) * openness;
    const outerY = sign * (.17 * arc + .008);
    for (let j = 0; j <= lid.V; j++) {
      const y = innerY + (outerY - innerY) * j / lid.V;
      const z = .15 * Math.sqrt(Math.max(0, 1 - (x / .19) ** 2 - (y / .175) ** 2)) + .012;
      const at = (i * (lid.V + 1) + j) * 3;
      positions[at] = x; positions[at + 1] = y; positions[at + 2] = z;
    }
  }
  lid.mesh.geometry.attributes.position.needsUpdate = true; lid.mesh.geometry.computeVertexNormals();
}
export function createPaw(parent, mat) {
  const root = new T.Group(); parent.add(root);
  ball(root, '圆钝犬爪掌', [0, 0, 0], [.24, .23, .18], mat);
  const toes = [];
  for (let i = -1; i <= 1; i++) toes.push(ball(root, '短犬趾-非人指', [i * .14, -.17, .09], [.085, .12, .10], mat));
  ball(root, '犬爪垫', [0, -.01, .16], [.12, .12, .04], material('#645651'));
  // Optional rooted short toes are invisible in legacy mode. Their length is
  // fixed and short relative to the palm; this is a canine gesture analogue,
  // not a recovered human index finger or a floating translated digit ball.
  const cueRoot = new T.Group(); cueRoot.name = '可选连接短犬趾'; cueRoot.visible = false; root.add(cueRoot);
  const cueToes = [];
  for (let i = -1; i <= 1; i++) {
    const base = new T.Group(); base.position.set(i * .14, -.115, .075); cueRoot.add(base);
    const proximalLength = .080, distalLength = .075;
    ball(base, '犬趾根-固定于掌内', [0, 0, 0], [.072, .065, .072], mat);
    segment(base, '短犬趾近节-连接', [0, 0, 0], [0, -proximalLength, 0], .055, mat);
    const distal = new T.Group(); distal.position.y = -proximalLength; base.add(distal);
    ball(distal, '短犬趾节-连接', [0, 0, 0], [.067, .057, .070], mat);
    segment(distal, '短犬趾末节-连接', [0, 0, 0], [0, -distalLength, 0], .056, mat);
    const tip = ball(distal, '圆钝短犬趾端', [0, -distalLength, 0], [.075, .070, .080], mat);
    cueToes.push({ index: i, base, distal, tip, proximalLength, distalLength });
  }
  let cueState = { mode: 'legacy_short_toe_balls', enabled: false, curl: null, fan: null };
  function applyCue(requested) {
    const enabled = requested !== null && typeof requested === 'object';
    const clamp = value => Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
    const curl = enabled ? clamp(requested.curl) : 0, fan = enabled ? clamp(requested.fan) : 0;
    const point = enabled ? clamp(requested.point) : 0;
    cueRoot.visible = enabled; toes.forEach(toe => { toe.visible = !enabled; });
    for (const toe of cueToes) {
      // Curl toward the pad through two fixed connected joints. Fan only the
      // outer short toes; no per-frame scaling or translation of a toe tip.
      const toeCurl=toe.index===0?curl*(1-point):Math.max(curl,point*.80);
      const outsideFold=toe.index===0?0:point;
      toe.base.rotation.set(-toeCurl * .72-outsideFold*.65, 0, toe.index * fan * .35);
      toe.distal.rotation.set(-toeCurl * .78-outsideFold*.45, 0, 0);
    }
    cueState = { mode: enabled ? 'rooted_connected_canine_short_toe_analogue' : 'legacy_short_toe_balls', enabled, curl: enabled ? curl : null, fan: enabled ? fan : null, point:enabled?point:null, fixedShortDigitLengths:true, humanFingerOneToOne: false };
    return cueState;
  }
  function geometryDiagnostics(dogRoot) {
    root.updateWorldMatrix(true, true);
    const toRoot = (mesh, point = new T.Vector3()) => dogRoot.worldToLocal(mesh.localToWorld(point)).toArray();
    const palmRootLocal = toRoot(root);
    const toeReports = cueToes.map(toe => {
      const base = toRoot(toe.base), joint = toRoot(toe.distal), tip = toRoot(toe.tip);
      return { index: toe.index, rootLocal: base, jointRootLocal: joint, tipRootLocal: tip,
        proximalLength: toe.proximalLength, distalLength: toe.distalLength,
        actualProximalLengthRootLocal: v(base).distanceTo(v(joint)), actualDistalLengthRootLocal: v(joint).distanceTo(v(tip)),
        fixedChainLength: toe.proximalLength + toe.distalLength, baseWithinPalm: Math.abs(toe.base.position.x) <= .24 && Math.abs(toe.base.position.y) <= .23 && Math.abs(toe.base.position.z) <= .18 };
    });
    const downRoot = new T.Vector3(0, -1, 0).applyQuaternion(root.quaternion).normalize().toArray();
    return { ...cueState, palmRootLocal, toeDirectionRootLocal: downRoot, connectedToesVisible: cueRoot.visible, legacyToesVisible: toes.every(toe => toe.visible),
      connectedToes: toeReports, adaptationOnly: true, sourceGraspVerified: false };
  }
  return { root, toes, cueToes, applyCue, geometryDiagnostics };
}
export function createDog(type = 'husky', tint = null) {
  const root = new T.Group();
  const coat = material(tint || (type === 'golden' ? '#ba8b4f' : '#424b57'));
  const light = material(type === 'golden' ? '#e3c18c' : '#e5e5df');
  const dark = material('#17202a'), mouth = material('#40242c');
  const body = new T.Group(); root.add(body);
  ball(body, '犬胸背', [0, 0, 0], [.56, .91, .37], coat);
  ball(body, '犬白胸', [0, -.05, .26], [.38, .71, .16], light);
  const pelvisMesh = ball(body, '犬骨盆', [0, -.86, -.03], [.49, .40, .35], coat);
  const tail = ball(body, '犬尾-不代替肢体', [.25, -.85, -.55], [.16, .50, .16], coat); tail.rotation.x = -.6; tail.rotation.z = -.5;
  const tailRestQuaternion = tail.quaternion.clone();
  const neck = new T.Group(); neck.position.set(0, .93, 0); body.add(neck);
  const head = new T.Group(); head.position.y = .33; neck.add(head);
  ball(head, '犬颅-稳定几何', [0, 0, 0], [.66, .64, .52], coat);
  ball(head, '白色鼻梁', [0, .15, .43], [.15, .43, .12], light);
  const cheeks = [];
  for (const s of [-1, 1]) {
    cheeks.push({ side: s, mesh: ball(head, '犬面颊', [s * .37, -.17, .33], [.29, .31, .25], light) });
    if (type === 'golden') {
      const ear = ball(head, '金毛垂耳', [s * .62, .10, -.06], [.23, .53, .15], coat); ear.rotation.z = -s * .18;
    } else {
      const ear = new T.Mesh(new T.ConeGeometry(.25, .65, 4), coat); ear.position.set(s * .43, .66, -.04); ear.rotation.z = -s * .15; head.add(ear);
    }
  }
  const legacyUpperMuzzle = ball(head, '固定口鼻上部', [0, -.20, .48], [.37, .19, .32], light);
  ball(head, '固定犬鼻', [0, -.10, .765], [.16, .10, .085], dark);
  const cavity = ball(head, '局部口腔', [0, -.34, .58], [.28, .11, .12], mouth);
  const jaw = new T.Group(); jaw.position.set(0, -.12, .22); head.add(jaw);
  const legacyLowerJaw = ball(jaw, '独立犬下颌', [0, -.24, .30], [.31, .13, .25], light);
  const corners = [-1, 1].map(s => segment(head, '局部嘴角线', [0, -.29, .77], [s * .31, -.30, .65], .014, dark));
  const eyes = [];
  for (const side of [-1, 1]) {
    const eye = new T.Group(); eye.position.set(side * .29, .16, .45); head.add(eye);
    ball(eye, '固定尺寸犬眼', [0, 0, 0], [.17, .135, .12], material('#eee9df'));
    const iris = ball(eye, '固定瞳孔', [0, 0, .115], [.069, .084, .023], material(type === 'golden' ? '#543e29' : '#7db9d2'));
    ball(iris, '瞳孔中心', [0, 0, .71], [.49, .65, .4], dark);
    // Curved eyelid shells follow the fixed eyeball envelope. No floating box
    // lids or changing eyeball scale. At openness=0 the shells meet at y=0.
    const upper = createEyeLid(eye, true, light), lower = createEyeLid(eye, false, light);
    const brow = segment(head, '犬眉弓', [side * .14, .37, .54], [side * .45, .35, .45], .035, coat);
    eyes.push({ side, group: eye, upper, lower, iris, brow });
  }
  const limbs = [];
  for (const side of [-1, 1]) {
    const arm = {
      side,
      upper: segment(root, '前肢上段-真实几何', [0, 0, 0], [0, -1, 0], .13, coat),
      lower: segment(root, '前肢下段-真实几何', [0, 0, 0], [0, -1, 0], .105, light),
      elbow: ball(root, '犬肘关节', [0, 0, 0], [.145, .145, .145], coat),
      shoulderJoint: ball(root, '重定向肩关节-固定半径', [0,0,0], [.17,.17,.17], coat),
      paw: createPaw(root, light),
    };
    const contactSide=side===-1?'screen_left_arm':'screen_right_arm';
    for(const part of ['upper','lower','elbow'])arm[part].userData.contactPart=contactSide+'_'+part;
    arm.shoulderJoint.userData.contactPart=contactSide+'_shoulder';arm.shoulderJoint.visible=false;
    const leg = {
      side,
      thigh: segment(root, '后肢大腿', [0, 0, 0], [0, -1, 0], .19, coat),
      shin: segment(root, '后肢胫段', [0, 0, 0], [0, -1, 0], .125, coat),
      hock: ball(root, '犬后肢关节', [0, 0, 0], [.15, .15, .15], coat),
      foot: ball(root, '承重犬后足', [0, 0, 0], [.19, .15, .34], light),
    };
    limbs.push({ arm, leg });
  }
  // This group is invisible by default and is built after every legacy mesh.
  // Disabled mode preserves the v4 draw geometry, ordering and transforms.
  const oralDisplay = createMouthDisplay(head, { fur: light, dark, oral: mouth, tooth: material('#e8dec8') });
  eyes.forEach(eye => {
    eye.closingCrease = createEyelidCrease(eye.upper.mesh.parent, dark);
    eye.faceGeometry = createFaceEyeGeometry(eye.group, head, eye.side, { fur: light, coat, iris: eye.iris.material, pupil: dark });
  });
  let faceDisplay = { enabled: false, engineeringDisplayOnly: true, sourceMeasurement: false };
  let faceDesignEnabled = true, lastPose = {};
  let faceDesignReport = { enabled: false, sourceMeasurement: false, visualAcceptance: false };
  const diagnostics = {};
  function apply(pose = {}) {
    lastPose = pose;
    const torso = torsoTransform(pose);
    body.position.copy(torso.position); body.quaternion.copy(torso.quaternion);
    if (pose.lockPelvis) {
      body.updateMatrix(); const inverse = body.matrix.clone().invert();
      pelvisMesh.position.copy(new T.Vector3(0, -.86, -.03).add(torso.base).applyMatrix4(inverse));
      pelvisMesh.quaternion.copy(torso.quaternion).invert();
      tail.position.copy(new T.Vector3(.25, -.85, -.55).add(torso.base).applyMatrix4(inverse));
      tail.quaternion.copy(torso.quaternion).invert().multiply(tailRestQuaternion);
    } else {
      pelvisMesh.position.set(0, -.86, -.03); pelvisMesh.quaternion.identity();
      tail.position.set(.25, -.85, -.55); tail.quaternion.copy(tailRestQuaternion);
    }
    neck.rotation.set((pose.headPitch || 0) * Math.PI / 180, (pose.headYaw || 0) * Math.PI / 180, (pose.headRoll || 0) * Math.PI / 180, 'YXZ');
    const face = pose.face || {}, open = Math.max(0, Math.min(1, face.mouth || 0)), smile = face.smile || 0;
    jaw.rotation.x = open * .75; cavity.scale.y = .08 + open * .17;
    jaw.scale.x = 1;
    const expression = face.expressionRetarget;
    const newDisplay = expression?.enabled === true;
    const designActive = faceDesignEnabled && pose.faceDesign?.enabled === true;
    const faceDesign = designActive ? pose.faceDesign : null;
    const mouthDisplay = oralDisplay.apply(expression, face, faceDesign);
    cavity.visible = !newDisplay; corners.forEach(mesh => { mesh.visible = !newDisplay; });
    legacyUpperMuzzle.visible = !newDisplay; legacyLowerJaw.visible = !newDisplay;
    if (newDisplay) {
      jaw.rotation.x = mouthDisplay.actual.jawRotationRadians;
      jaw.scale.x = .92 + .08 * mouthDisplay.requested.mouthWidth - .08 * mouthDisplay.requested.lipRound;
    }
    cheeks.forEach(({ side, mesh }) => {
      const corner = newDisplay ? (side === 1 ? mouthDisplay.requested.cornerLeft : mouthDisplay.requested.cornerRight) : 0;
      const cheek = faceDesign?.cheeks?.[side === 1 ? 'Left' : 'Right'] || {};
      const lift = Number.isFinite(cheek.lift) ? Math.max(-.025, Math.min(.05, cheek.lift)) : 0;
      const forward = Number.isFinite(cheek.forward) ? Math.max(-.025, Math.min(.04, cheek.forward)) : 0;
      mesh.position.set(side * (.37 + (newDisplay ? .008 * corner : 0)), -.17 + .035 * corner + lift, .33 + (newDisplay ? .012 * mouthDisplay.requested.lipRound : 0) + forward);
    });
    corners.forEach((mesh, i) => {
      const cornerSmile = i ? (face.smileLeft ?? smile) : (face.smileRight ?? smile);
      setSegment(mesh, [0, -.29, .77], [(i ? 1 : -1) * (.31 - (face.round || 0) * .06), -.30 + cornerSmile * .12, .65], .014);
    });
    const actualEyes = {}, designedEyes = {};
    const scalar = (value, low, high, fallback = 0) => Number.isFinite(value) ? Math.max(low, Math.min(high, value)) : fallback;
    eyes.forEach(eye => {
      const sideKey = eye.side === 1 ? 'eyeL' : 'eyeR';
      const anatomical = eye.side === 1 ? 'Left' : 'Right';
      const aperture = newDisplay && Number.isFinite(expression[`eye${anatomical}`]) ? scalar(expression[`eye${anatomical}`], 0, 1) : face[sideKey] == null ? .8 : Math.max(0, Math.min(1, face[sideKey]));
      setEyeLid(eye.upper, aperture); setEyeLid(eye.lower, aperture);
      eye.iris.position.x = newDisplay && Number.isFinite(expression.gazeX) ? scalar(expression.gazeX, -1, 1) * .035 : (face[`gazeX${anatomical}`] ?? face.gazeX ?? 0) * .035;
      eye.iris.position.y = newDisplay && Number.isFinite(expression.gazeY) ? scalar(expression.gazeY, -1, 1) * .028 : (face[`gazeY${anatomical}`] || 0) * .028;
      const innerUp = face.browInnerUp || 0, outerUp = face[`browOuterUp${anatomical}`] || 0, down = face[`browDown${anatomical}`] || 0;
      const innerPoint = newDisplay ? [eye.side * .14, .37 + .14 * scalar(expression.browInner, 0, 1) - .10 * scalar(expression[`browDown${anatomical}`], 0, 1), .54] : [eye.side * .14, .37 + .07 * innerUp - .05 * down, .54];
      const outerPoint = newDisplay ? [eye.side * .45, .35 + .12 * scalar(expression[`browOuter${anatomical}`], 0, 1) - .07 * scalar(expression[`browDown${anatomical}`], 0, 1), .45] : [eye.side * .45, .35 + .07 * outerUp - .035 * down, .45];
      setSegment(eye.brow, innerPoint, outerPoint, .035);
      eye.upper.mesh.visible = !designActive; eye.lower.mesh.visible = !designActive;
      eye.iris.visible = !designActive; eye.brow.visible = !designActive;
      eye.iris.position.z = .115; eye.iris.rotation.set(0, 0, 0);
      const gazeControl = faceDesign?.gaze || {};
      const gaze = { x: scalar(gazeControl.x, -1, 1, scalar(expression?.gazeX, -1, 1)), y: scalar(gazeControl.y, -1, 1, scalar(expression?.gazeY, -1, 1)) };
      if (designActive && Array.isArray(gazeControl.targetWorld) && gazeControl.targetWorld.length === 3 && gazeControl.targetWorld.every(Number.isFinite)) {
        eye.group.updateWorldMatrix(true, false);
        const direction = eye.group.worldToLocal(new T.Vector3(...gazeControl.targetWorld));
        if (direction.lengthSq() > 1e-10) {
          gaze.yaw = Math.atan2(direction.x, direction.z);
          gaze.pitch = Math.atan2(direction.y, Math.hypot(direction.x, direction.z));
          gaze.targetWorld = [...gazeControl.targetWorld]; gaze.directionLocal = direction.clone().normalize().toArray();
        }
      }
      const eyeControl = faceDesign?.eyes?.[anatomical] || {}, cheekControl = faceDesign?.cheeks?.[anatomical] || {};
      const designedEye = eye.faceGeometry.apply(designActive, aperture, { ...eyeControl, lowerOuterLift: scalar(eyeControl.lowerOuterLift, -.03, .05) + .35 * scalar(cheekControl.lift, -.025, .05) }, faceDesign?.brows?.[anatomical], gaze, { inner: innerPoint, outer: outerPoint });
      designedEyes[anatomical] = designedEye;
      actualEyes[anatomical] = { eyeOpenness: aperture, irisOffsetLocal: eye.iris.position.toArray(), browInnerEndpointLocal: innerPoint, browOuterEndpointLocal: outerPoint, closingCrease: eye.closingCrease.apply(aperture, newDisplay && !designActive), faceDesign: designedEye };
    });
    faceDesignReport = { enabled: designActive, switchEnabled: faceDesignEnabled, poseRequested: pose.faceDesign?.enabled === true,
      mode: designActive ? 'v12_existing_breed_local_face_geometry' : 'v10_geometry_replayed', sourceMeasurement: false, visualAcceptance: false,
      mouth: mouthDisplay.faceDesign || { enabled: false }, eyes: designedEyes,
      cheekCentersLocal: Object.fromEntries(cheeks.map(({ side, mesh }) => [side === 1 ? 'Left' : 'Right', mesh.position.toArray()])),
      oldLidsVisible: eyes.every(eye => eye.upper.mesh.visible && eye.lower.mesh.visible), oldIrisVisible: eyes.every(eye => eye.iris.visible), oldBrowsVisible: eyes.every(eye => eye.brow.visible),
      fixedSkullNoseEyeGeometry: true, bodyHeadPropsSourceClockChanged: false, authoredOnly: true };
    faceDisplay = { ...mouthDisplay, mode: designActive ? 'v12_existing_breed_authored_face_geometry_NOT_measurement' : newDisplay ? 'authored_canine_local_expression_display_NOT_measurement' : 'legacy_v4_display_unchanged',
      rawFacePreserved: true, rawMouthCoefficient: face.mouth ?? null, jawRotationRadians: jaw.rotation.x,
      legacyCavityVisible: cavity.visible, legacyCornerLinesVisible: corners.every(mesh => mesh.visible), legacyUpperMuzzleVisible: legacyUpperMuzzle.visible, legacyLowerJawVisible: legacyLowerJaw.visible,
      fixedSkullNoseEyeGeometry: true, muzzleEnvelopeAnchoredToLegacy: true,
      actualEyes, actualCheekCentersLocal: Object.fromEntries(cheeks.map(({ side, mesh }) => [side === 1 ? 'Left' : 'Right', mesh.position.toArray()])),
      mapping: { side: 'anatomical Left = positive head-local x = screen-right when frontal', brows: newDisplay ? 'inner=.14*browInner-.10*browDown; outer=.12*browOuter-.07*browDown' : 'legacy_v4', cheeks: newDisplay ? 'local y+=.035*corner; x+=side*.008*corner; z+=.012*lipRound' : 'legacy_v4', eyeGeometry: 'fixed eyeball dimensions; existing curved occluding eyelids; no autonomous blink' },
      visualAcceptance: false };
    for (const { arm, leg } of limbs) {
      const s = arm.side;
      const shoulderArc=pose[s===-1?'shoulderArcLeft':'shoulderArcRight']||0;
      // Fixed-size shoulder socket on the thorax envelope. This retargeting
      // degree of freedom does not resize the chest or stretch arm segments.
      let shoulder = new T.Vector3(s * .58*Math.cos(shoulderArc), .54, .39*Math.sin(shoulderArc)).applyEuler(body.rotation).add(body.position);
      const shoulderOffset=pose[s===-1?'shoulderOffsetLeft':'shoulderOffsetRight'];
      if(Array.isArray(shoulderOffset))shoulder.add(v(shoulderOffset));
      let target = pose[s === -1 ? 'handScreenLeft' : 'handScreenRight'];
      if (!target) target = [s * .68, -.72, .1];
      target = v(target);
      if (pose[s === -1 ? 'targetSpaceLeft' : 'targetSpaceRight'] !== 'root') target.applyEuler(body.rotation).add(body.position);
      const scapula = Math.max(0, Math.min(.09, (target.y - shoulder.y) * .035));
      shoulder.y += scapula;
      // Fixed biped-staging forelimb proportions. These are not recovered
      // canine anatomical measurements and are never stretched per frame.
      const limbLength = 1.65;
      const delta = target.clone().sub(shoulder), maximum = limbLength * 2 - .02, requestedDistance = Math.max(.15, delta.length());
      const transition = .18;
      let distance = requestedDistance;
      if (requestedDistance > maximum + transition) distance = maximum;
      else if (requestedDistance > maximum - transition) { const u = (requestedDistance - maximum + transition) / (2 * transition); distance = maximum - transition + 2 * transition * (u - .5 * u * u); }
      const direction = delta.clone().normalize();
      const tuck = Math.max(0, Math.min(1, pose.elbowTuck || 0));
      const explicitPole = pose[s === -1 ? 'elbowPoleLeft' : 'elbowPoleRight'];
      const bend = explicitPole ? new T.Vector3(...explicitPole) : new T.Vector3(0, 0, .8).add(new T.Vector3(s * .25 * (1 - tuck), -.15 * (1 - tuck), 0));
      bend.addScaledVector(direction, -bend.dot(direction));
      const poleProjectionLength = bend.length();
      if (bend.lengthSq() < 1e-8) bend.set(1, 0, 0).addScaledVector(direction, -direction.x);
      bend.normalize();
      const elbow = shoulder.clone().addScaledVector(direction, distance * .5).addScaledVector(bend, Math.sqrt(Math.max(0, limbLength ** 2 - (distance * .5) ** 2)));
      const wrist = shoulder.clone().addScaledVector(direction, distance);
      setSegment(arm.upper, shoulder.toArray(), elbow.toArray(), .13);
      arm.shoulderJoint.position.copy(shoulder);arm.shoulderJoint.visible=pose[s===-1?'shoulderJointLeft':'shoulderJointRight']===true;
      setSegment(arm.lower, elbow.toArray(), wrist.toArray(), .105);
      arm.elbow.position.copy(elbow); arm.paw.root.position.copy(wrist);
      arm.paw.root.quaternion.setFromUnitVectors(new T.Vector3(0, -1, 0), wrist.clone().sub(elbow).normalize());
      if (pose[s === -1 ? 'wristUprightLeft' : 'wristUprightRight']) arm.paw.root.quaternion.identity();
      arm.paw.root.rotateZ(pose[s === -1 ? 'wristLeftRoll' : 'wristRightRoll'] || 0);
      const pawDirection = pose[s === -1 ? 'pawDirectionLeft' : 'pawDirectionRight'];
      const validPawDirection = Array.isArray(pawDirection) && pawDirection.length === 3 && pawDirection.every(Number.isFinite) && v(pawDirection).lengthSq() > 1e-10;
      // Explicit direction is dog-root local and replaces the legacy wrist
      // orientation including its roll, so (0,-1,0) really faces this vector.
      if (validPawDirection) arm.paw.root.quaternion.setFromUnitVectors(new T.Vector3(0, -1, 0), v(pawDirection).normalize());
      const pawCue = arm.paw.applyCue(pose[s === -1 ? 'pawCueLeft' : 'pawCueRight']);
      const digit = s === -1 ? (pose.digit || 0) : 0;
      arm.paw.toes[1].position.y = -.17 + (pawCue.enabled ? 0 : digit * .42);
      arm.paw.toes[1].rotation.x = pawCue.enabled ? 0 : -digit * .55;
      const hip = pose.lockPelvis ? new T.Vector3(s * .32, -.85, 0).add(torso.base) : new T.Vector3(s * .32, -.85, 0).applyEuler(body.rotation).add(body.position);
      const requestedFoot = v(pose.feetLocal?.[s === -1 ? 'left' : 'right'] || [s * .40, -2.75, .12]);
      const legLength = .96, legDelta = requestedFoot.clone().sub(hip), legDistance = Math.max(.02, Math.min(legLength * 2 - .015, legDelta.length()));
      const legDirection = legDelta.clone().normalize(), kneePole = new T.Vector3(0, 0, 1);
      kneePole.addScaledVector(legDirection, -kneePole.dot(legDirection));
      if (kneePole.lengthSq() < 1e-8) kneePole.set(1, 0, 0).addScaledVector(legDirection, -legDirection.x);
      kneePole.normalize();
      const knee = hip.clone().addScaledVector(legDirection, legDistance * .5).addScaledVector(kneePole, Math.sqrt(Math.max(0, legLength ** 2 - (legDistance * .5) ** 2)));
      const foot = hip.clone().addScaledVector(legDirection, legDistance);
      setSegment(leg.thigh, hip.toArray(), knee.toArray(), .19);
      setSegment(leg.shin, knee.toArray(), foot.toArray(), .125);
      leg.hock.position.copy(knee); leg.foot.position.copy(foot);
      diagnostics[s === -1 ? 'screen_left_arm' : 'screen_right_arm'] = { shoulder: shoulder.toArray(), elbow: elbow.toArray(), wrist: wrist.toArray(), requestedTarget: target.toArray(), scapulaElevation: scapula, wristFollowsForearm: !validPawDirection, fixedSegmentLength: limbLength, reachLimited: delta.length() > limbLength * 2 - .02, targetErrorLocal: wrist.distanceTo(target), explicitElbowPole: explicitPole || null, poleProjectionLength,
        pawGesture: { ...arm.paw.geometryDiagnostics(root), requestedDirectionRootLocal: validPawDirection ? [...pawDirection] : null, explicitDirectionApplied: validPawDirection, legacyDigitSuppressed: pawCue.enabled } };
      diagnostics[s === -1 ? 'left_leg' : 'right_leg'] = { hip: hip.toArray(), knee: knee.toArray(), actualFoot: foot.toArray(), requestedFoot: requestedFoot.toArray(), fixedSegmentLength: legLength, reachLimited: legDelta.length() > legLength * 2 - .015, targetErrorLocal: foot.distanceTo(requestedFoot) };
    }
    // Export callers retain each frame. Never return this mutable working
    // object: later seeks must not rewrite an earlier frame's limb report.
    return structuredClone(diagnostics);
  }
  apply();
  function worldDiagnostics(pose) {
    root.updateWorldMatrix(true, true);
    const footReports = {};
    for (const { leg } of limbs) {
      const side = leg.side === -1 ? 'left' : 'right', local = diagnostics[`${side}_leg`];
      const actualWorld = leg.foot.getWorldPosition(new T.Vector3()), requestedWorld = root.localToWorld(v(local.requestedFoot));
      const scale = root.getWorldScale(new T.Vector3());
      footReports[side] = { actualWorldFoot: actualWorld.toArray(), requestedWorldFoot: requestedWorld.toArray(), worldError: actualWorld.distanceTo(requestedWorld), worldErrorVector: actualWorld.clone().sub(requestedWorld).toArray(), supportRequested: pose.contactPlan?.support?.[side] ?? null, legReachLimited: local.reachLimited,
        groundPlaneY: pose.contactPlan?.floorY ?? null, actualSoleGroundError: pose.contactPlan ? actualWorld.y - .15 * Math.abs(scale.y) - pose.contactPlan.floorY : null,
        contactVerified: false, note: 'Only constraint error is computed; original source footfalls are not recovered.' };
    }
    const arms = {};
    for (const side of ['left', 'right']) {
      const local = diagnostics[`screen_${side}_arm`];
      const point = key => root.localToWorld(v(local[key])).toArray();
      arms[`screen_${side}_arm`] = { shoulderWorld: point('shoulder'), elbowWorld: point('elbow'), wristWorld: point('wrist'), requestedWristWorld: point('requestedTarget'), reachLimited: local.reachLimited, actualWorldTargetError: local.targetErrorLocal * Math.abs(root.getWorldScale(new T.Vector3()).x) };
      const pawGesture = structuredClone(local.pawGesture);
      const worldPoint = p => root.localToWorld(v(p)).toArray();
      arms[`screen_${side}_arm`].pawGesture = { ...pawGesture, palmWorld: worldPoint(pawGesture.palmRootLocal),
        connectedToesWorld: pawGesture.connectedToes.map(toe => ({ index: toe.index, rootWorld: worldPoint(toe.rootLocal), jointWorld: worldPoint(toe.jointRootLocal), tipWorld: worldPoint(toe.tipRootLocal) })) };
    }
    return { rootWorld: root.getWorldPosition(new T.Vector3()).toArray(), pelvisWorld: pelvisMesh.getWorldPosition(new T.Vector3()).toArray(), bodyWorld: body.getWorldPosition(new T.Vector3()).toArray(), headCenterWorld: head.getWorldPosition(new T.Vector3()).toArray(), bodyForwardWorld: body.getWorldDirection(new T.Vector3()).toArray(), headForwardWorld: head.getWorldDirection(new T.Vector3()).toArray(), pelvisLockedForAuthoredTorsoPivot: !!pose.lockPelvis, arms, feet: footReports, faceDisplay: structuredClone(faceDisplay), faceDesign: structuredClone(faceDesignReport) };
  }
  function setFaceDesignEnabled(enabled) {
    faceDesignEnabled = enabled === true;
    apply(lastPose);
    return structuredClone(faceDesignReport);
  }
  return { root, body, head, neck, apply, type, diagnostics, worldDiagnostics, getPawPad: side => limbs.find(item => item.arm.side === (side === 'screen_right_arm' ? 1 : -1)).arm.paw.root.getObjectByName('犬爪垫'), faceDiagnostics: () => structuredClone(faceDisplay), setFaceDesignEnabled, faceDesignDiagnostics: () => structuredClone(faceDesignReport) };
}
