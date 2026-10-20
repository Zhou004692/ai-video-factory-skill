// Portable screen-space 2.5D adapter. Never imports a source-shot stage/motion.
import * as T from './vendor/three.module.js';
import {createDog,setSegment} from './rig.js';
import {createAnimalBody, updateAnimalBody} from './animal-body.js';
import {solveProjectedChain} from './projected-limbs.mjs';

const input = await (await fetch('/scene.json')).json();
const canvas = document.querySelector('canvas');
const renderer = new T.WebGLRenderer({canvas, antialias:true, preserveDrawingBuffer:true});
renderer.setPixelRatio(1);
renderer.outputColorSpace = T.SRGBColorSpace;
const gl = renderer.getContext();
const debug = gl.getExtension('WEBGL_debug_renderer_info');
const scene = new T.Scene();
scene.background = new T.Color('#111923');
scene.add(new T.HemisphereLight('#e9f2ff','#756d61',2.5));
const light = new T.DirectionalLight('#fff3df',3);
light.position.set(-200,500,800);scene.add(light);
const camera = new T.OrthographicCamera(0,input.width,input.height,0,.1,10000);
camera.position.set(0,0,3000);
const clamp = (value,min,max) => Math.max(min,Math.min(max,value));
const dist = (a,b) => Math.hypot(a[0]-b[0],a[1]-b[1]);
const actors = input.roles.map(role => {
  const dog = createDog(role.animal);
  dog.root.name = role.id;
  scene.add(dog.root);
  const find = name => dog.root.children.filter(part=>part.name===name);
  const arms = [0,1].map(index=>({
    upper:find('前肢上段-真实几何')[index],lower:find('前肢下段-真实几何')[index],
    elbow:find('犬肘关节')[index],shoulder:find('重定向肩关节-固定半径')[index],
    paw:dog.getPawPad(index?'screen_right_arm':'screen_left_arm').parent,
  }));
  const legs = [0,1].map(index=>({upper:find('后肢大腿')[index],lower:find('后肢胫段')[index],
    knee:find('犬后肢关节')[index],foot:find('承重犬后足')[index]}));
  // Keep the reused authored coat/body geometry in a fixed torso frame.
  // The body/head hierarchy retains a connected cervical attachment.
  dog.body.children[0].visible=false;
  dog.body.children[1].visible=false;
  dog.body.children[3].visible=false;
  const body = createAnimalBody(T,dog.body,role.animal==='golden'?1:0);
  const Q=Array.from({length:17},()=>new T.Vector3());
  Q[0].set(0,-.85,0);Q[8].set(0,.54,0);
  const shape=role.fixed_projected_shape;
  Q[14].set(-shape.shoulder_width/2,.54,0);Q[11].set(shape.shoulder_width/2,.54,0);
  Q[1].set(-shape.hip_width/2,-.85,0);Q[4].set(shape.hip_width/2,-.85,0);
  updateAnimalBody(T,body,Q,new T.Vector3(0,1,0),new T.Vector3(1,0,0),new T.Vector3(0,0,1),shape.shoulder_width,shape.hip_width,1.39,.13);
  dog.body.children[2].scale.set(shape.hip_width*.57,shape.torso*.13,shape.hip_width*.36);
  // One mapping in the SAME de-rolled coordinate domain as the sockets and
  // targets. Inverted actors must not swap sides from raw screen-x sorting.
  const leftFirst=role.source_side_mapping.left_first;
  return {...role,dog,arms,legs,body,leftFirst};
});

function drive(actor, record) {
  const dog=actor.dog, q=record.points, visible={...record.visibility};
  if(actor.leftFirst===null)for(const key of ['arm_left','arm_right','leg_left','leg_right'])visible[key]=false;
  dog.root.visible=visible.body;
  if(!visible.body)return {id:actor.id,hidden:true,missing:visible};
  const s=actor.fixed_pixels_per_unit;
  const shoulder=[(q[5][0]+q[6][0])/2,(q[5][1]+q[6][1])/2];
  const hip=[(q[11][0]+q[12][0])/2,(q[11][1]+q[12][1])/2];
  const roll=Math.atan2(-(shoulder[0]-hip[0]),hip[1]-shoulder[1]);
  const c=Math.cos(roll),sn=Math.sin(roll);
  // Root/landmarks and camera all share crop-relative pixel coordinates.
  const root=[hip[0]-.85*s*sn,input.height-hip[1]+.85*s*c];
  dog.root.position.set(root[0],root[1],0);
  dog.root.scale.setScalar(s);dog.root.rotation.set(0,0,roll);
  const local=p=>{
    const x=(p[0]-root[0])/s,y=(input.height-p[1]-root[1])/s;
    return [c*x+sn*y,-sn*x+c*y,0];
  };
  const pose={lockPelvis:true,face:{mouth:0,eyeL:1,eyeR:1}};
  const metrics={id:actor.id,hidden:false,root_image_xy:[root[0],input.height-root[1]],
    fixed_pixels_per_unit:s,body_roll_radians:roll,depth_policy:'all observed targets placed in image plane z=0; not inferred physical depth',
    missing_groups:Object.keys(visible).filter(k=>!visible[k]),controls:{body:'2D measured torso/root',face:'unavailable neutral authored display',hands:'unavailable neutral canine paws',feet:'2D ankles only'}};
  for(let index=0;index<2;index++){
    const left=index===0?actor.leftFirst:!actor.leftFirst;
    const side=index===0?'Left':'Right';
    const sourceSide=left?'left':'right';
    const si=left?5:6,ei=left?7:8,wi=left?9:10;
    const hi=left?11:12,ki=left?13:14,ai=left?15:16;
    const armAvailable=visible['arm_'+sourceSide],legAvailable=visible['leg_'+sourceSide];
    if(armAvailable){
      pose['handScreen'+side]=local(q[wi]);pose['targetSpace'+side]='root';
      const e=local(q[ei]),sh=local(q[si]),w=local(q[wi]);
      pose['elbowPole'+side]=e.map((value,k)=>value-(sh[k]+w[k])/2);
      // Finger curl/fan affects only the connected, fixed short canine toes.
      if(visible['hand_'+sourceSide]){
        const h=q.slice(left?91:112,left?112:133),span=Math.max(.001,dist(h[5],h[17]));
        const openness=[8,12,16,20].reduce((sum,j)=>sum+dist(h[0],h[j]),0)/(4*span);
        pose['pawCue'+side]={curl:clamp(1-openness/2.5,0,1),fan:clamp(dist(h[8],h[20])/(span*2),0,1),point:0};
        metrics.controls.hands='measured WholeBody21 2D curl/fan mapped to fixed connected canine toes; no human finger reconstruction';
      }
    }
    if(legAvailable){
      pose.feetLocal??={};pose.feetLocal[index===0?'left':'right']=local(q[ai]);
    }
    // Missing limbs are removed for this frame, never held from a neighbor.
    for(const part of Object.values(actor.arms[index]))part.visible=armAvailable;
    for(const part of Object.values(actor.legs[index]))part.visible=legAvailable;
  }
  if(visible.head){
    const eyeAngle=Math.atan2(q[2][1]-q[1][1],q[2][0]-q[1][0]);
    // Head roll is planar; no yaw/depth invented from a face direction guess.
    pose.headRoll=clamp(-Math.atan2(Math.sin(eyeAngle),Math.abs(Math.cos(eyeAngle)))*180/Math.PI-roll*180/Math.PI,-70,70);
    metrics.controls.head='eye-line roll only; yaw/pitch unavailable';
  }
  if(visible.face){
    const f=q.slice(23,91),width=Math.max(.001,dist(f[48],f[54]));
    pose.face.mouth=clamp(dist(f[62],f[66])/width*2,0,1);
    pose.face.eyeL=clamp((dist(f[37],f[41])+dist(f[38],f[40]))/Math.max(.001,dist(f[36],f[39]))*2,0,1);
    pose.face.eyeR=clamp((dist(f[43],f[47])+dist(f[44],f[46]))/Math.max(.001,dist(f[42],f[45]))*2,0,1);
    metrics.controls.face='2D measured mouth aperture/eye aperture canine analogue; no inferred emotion or autonomous blink';
  }
  dog.apply(pose);
  // The accepted source stage used dog.apply for expression, then overrode
  // limb meshes from a separate fixed-shape skeleton. Its template 1.65 arm
  // length was never the final staged limb shape. Reproduce that separation
  // with this source's one fixed projected shape, not old shot motion data.
  const diagnostics={},limbGeometry={};
  const shape=actor.fixed_projected_shape;
  for(let index=0;index<2;index++){
    const left=index===0?actor.leftFirst:!actor.leftFirst;
    const sourceSide=left?'left':'right',sign=index===0?-1:1;
    for(const kind of ['arm','leg']){
      const key=kind+'_'+sourceSide,proportions=shape.limbs[key];
      const indices=kind==='arm'?(left?[5,7,9]:[6,8,10]):(left?[11,13,15]:[12,14,16]);
      const socket=[sign*(kind==='arm'?shape.shoulder_width:shape.hip_width)/2,kind==='arm'?.54:-.85,0];
      let solved={available:false,reason:'missing reliable observation or fixed shape'};
      if(visible[key]&&proportions.available){
        if(dist(q[indices[0]],q[indices[1]])>1e-6&&dist(q[indices[1]],q[indices[2]])>1e-6)
          solved=solveProjectedChain(socket,local(q[indices[1]]),local(q[indices[2]]),proportions.upper,proportions.lower);
        else solved={available:false,reason:'degenerate observed projected segment'};
      }
      visible[key]=visible[key]&&solved.available;
      limbGeometry[key]={visible:visible[key],...solved,shape_provenance:shape.provenance};
      if(!visible[key])continue;
      const parts=kind==='arm'?actor.arms[index]:actor.legs[index];
      setSegment(parts.upper,solved.root,solved.joint,kind==='arm'?.13:.19);
      setSegment(parts.lower,solved.joint,solved.tip,kind==='arm'?.105:.125);
      limbGeometry[key].actual_mesh_upper_length=parts.upper.scale.y;
      limbGeometry[key].actual_mesh_lower_length=parts.lower.scale.y;
      if(kind==='arm'){
        parts.elbow.position.set(...solved.joint);
        parts.paw.position.set(...solved.tip);
        parts.paw.quaternion.setFromUnitVectors(new T.Vector3(0,-1,0),new T.Vector3(...solved.tip).sub(new T.Vector3(...solved.joint)).normalize());
        diagnostics[index?'screen_right_arm':'screen_left_arm']={shoulder:solved.root,elbow:solved.joint,wrist:solved.tip,
          requestedTarget:solved.requestedTarget,fixedSegmentLengths:[proportions.upper,proportions.lower],
          targetErrorLocal:solved.target_error_local,reachLimited:solved.reach_limited};
      }else{
        parts.knee.position.set(...solved.joint);parts.foot.position.set(...solved.tip);
        diagnostics[index?'right_leg':'left_leg']={hip:solved.root,knee:solved.joint,actualFoot:solved.tip,
          requestedFoot:solved.requestedTarget,fixedSegmentLengths:[proportions.upper,proportions.lower],
          targetErrorLocal:solved.target_error_local,reachLimited:solved.reach_limited};
      }
    }
  }
  dog.neck.visible=visible.head;
  // apply() may change mesh visibility; restore missing group boundaries.
  for(let index=0;index<2;index++){
    const left=index===0?actor.leftFirst:!actor.leftFirst;
    const sourceSide=left?'left':'right';
    for(const [key,part] of Object.entries(actor.arms[index]))part.visible=visible['arm_'+sourceSide]&&key!=='shoulder';
    for(const part of Object.values(actor.legs[index]))part.visible=visible['leg_'+sourceSide];
  }
  dog.root.updateWorldMatrix(true,true);
  metrics.ik=diagnostics;
  metrics.limb_geometry=limbGeometry;
  metrics.fixed_projected_shape=shape;
  metrics.source_side_mapping=actor.source_side_mapping;
  metrics.missing_groups=Object.keys(visible).filter(k=>!visible[k]);
  metrics.landmark_residuals_pixels=[];
  for(let index=0;index<2;index++){
    const left=index===0?actor.leftFirst:!actor.leftFirst;
    const sourceSide=left?'left':'right';
    const arm=diagnostics[index?'screen_right_arm':'screen_left_arm'];
    const leg=diagnostics[index?'right_leg':'left_leg'];
    const comparisons=[];
    if(visible['arm_'+sourceSide])comparisons.push(['elbow',left?7:8,arm.elbow],['wrist',left?9:10,arm.wrist]);
    if(visible['leg_'+sourceSide])comparisons.push(['knee',left?13:14,leg.knee],['ankle',left?15:16,leg.actualFoot]);
    for(const [name,key,localPoint] of comparisons){
      const p=dog.root.localToWorld(new T.Vector3(...localPoint));
      const image=[p.x,input.height-p.y];
      metrics.landmark_residuals_pixels.push({side:sourceSide,landmark:name,source_xy:q[key],rendered_xy:image,error_pixels:dist(image,q[key])});
    }
  }
  metrics.neck_connection='authored fixed body -> neck -> head hierarchy; no detached head';
  const bbox=new T.Box3();
  dog.root.traverse(object=>{
    if(!object.geometry)return;
    for(let parent=object;parent;parent=parent.parent)if(!parent.visible)return;
    object.geometry.computeBoundingBox();
    bbox.union(object.geometry.boundingBox.clone().applyMatrix4(object.matrixWorld));
  });
  metrics.bounds_image_xyxy=[bbox.min.x,input.height-bbox.max.y,bbox.max.x,input.height-bbox.min.y];
  return metrics;
}

const allMetrics=[];
let xmin=0,ymin=0,xmax=input.width,ymax=input.height;
for(const frame of input.frames){
  const metrics=actors.map(actor=>drive(actor,frame.roles.find(r=>r.id===actor.id)));
  for(const metric of metrics){
    if(metric.hidden)continue;
    const b=metric.bounds_image_xyxy;xmin=Math.min(xmin,b[0]);ymin=Math.min(ymin,b[1]);xmax=Math.max(xmax,b[2]);ymax=Math.max(ymax,b[3]);
  }
  allMetrics.push({frame_index:frame.index,pts_seconds:frame.pts_seconds,roles:metrics});
}
// A single full-clip viewport fits all canine geometry while preserving shared
// image coordinates; the original crop receives exactly the same pad/scale.
const margin=Math.max(input.width,input.height)*.025;
xmin=Math.floor(xmin-margin);ymin=Math.floor(ymin-margin);
xmax=Math.ceil(xmax+margin);ymax=Math.ceil(ymax+margin);
let vw=xmax-xmin,vh=ymax-ymin;
if(vw%2)vw++;if(vh%2)vh++;
const width=input.render_width,height=Math.max(2,Math.round(width*vh/vw/2)*2);
renderer.setSize(width,height,false);
camera.left=xmin;camera.right=xmin+vw;camera.top=input.height-ymin;camera.bottom=input.height-ymin-vh;camera.updateProjectionMatrix();
const info={width,height,viewport:{x:xmin,y:ymin,width:vw,height:vh},three_revision:T.REVISION,
  renderer:debug?gl.getParameter(debug.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER),
  capture:'owned task WebGL canvas.toDataURL; no desktop/window capture',
  mode:input.mode,frame_count:input.frames.length,frame_records:allMetrics,
  depth_recovered:false,contact_verified:false,bone_lengths_changed_per_frame:false,
  full_clip_viewport:true,missing_geometry_policy:input.missing_policy,
  maximum_landmark_residual_pixels:allMetrics.reduce((maximum,frame)=>frame.roles.reduce((current,role)=>(role.landmark_residuals_pixels||[]).reduce((value,entry)=>Math.max(value,entry.error_pixels),current),maximum),0)};
window.taskRenderer={info,renderFrame(index){
  const frame=input.frames[index];
  if(!frame)throw Error('Frame outside measured coverage');
  actors.forEach(actor=>drive(actor,frame.roles.find(r=>r.id===actor.id)));
  renderer.render(scene,camera);
  return canvas.toDataURL('image/png').split(',')[1];
}};
