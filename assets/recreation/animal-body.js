// Authored canine body for the same bipedal dance skeleton. No new source
// observations, pose interpolation, cloth, or paid/generated media are used.
const RINGS=11, SIDES=32;
// Neck opening shares the reviewed thorax+0.12*torso attachment height.
// A tapered collar leaves room for the existing low head poses instead of
// moving the head or changing the source choreography to fit a wider chest.
const Y=[-.12,-.04,0,.16,.35,.53,.70,.86,1,1.14,1.22];
const W=[.035,.18,.30,.42,.43,.37,.33,.32,.31,.22,.035];
const DEPTH=[.7,.76,.76,.78,.80,.82,.86,.9,.92,.9,.75];
export function createAnimalBody(T,root,index){
 const coat=new T.Color(index?'#ba8b4f':'#424b57'),cream=new T.Color(index?'#e3c18c':'#e5e5df');
 const geo=new T.BufferGeometry();
 geo.setAttribute('position',new T.BufferAttribute(new Float32Array(RINGS*SIDES*3),3));
 geo.setAttribute('color',new T.BufferAttribute(new Float32Array(RINGS*SIDES*3),3));
 const ix=[];
 for(let r=0;r<RINGS-1;r++)for(let j=0;j<SIDES;j++){const a=r*SIDES+j,b=r*SIDES+(j+1)%SIDES,c=a+SIDES,d=b+SIDES;ix.push(a,c,b,b,c,d);}
 for(let j=1;j<SIDES-1;j++){ix.push(0,j,j+1);const b=(RINGS-1)*SIDES;ix.push(b,b+j+1,b+j);}
 geo.setIndex(ix);
 const surface=new T.Mesh(geo,new T.MeshStandardMaterial({vertexColors:true,roughness:.97,side:T.DoubleSide}));
 surface.name=index?'金毛-无衣服犬胸腹闭合表面':'哈士奇-无衣服犬胸腹闭合表面';root.add(surface);
 const hairGeo=new T.BufferGeometry();hairGeo.setAttribute('position',new T.BufferAttribute(new Float32Array(960*6),3));hairGeo.setAttribute('color',new T.BufferAttribute(new Float32Array(960*6),3));
 const tufts=new T.LineSegments(hairGeo,new T.LineBasicMaterial({vertexColors:true,transparent:true,opacity:.55}));
 tufts.name='装饰短被毛-不视为已清除碰撞';tufts.frustumCulled=false;root.add(tufts);
 const sockets=[];
 for(let k=0;k<4;k++){
  const m=new T.Mesh(new T.SphereGeometry(1,16,12),new T.MeshStandardMaterial({color:coat,roughness:1}));m.name=k<2?'犬肩胸过渡':'犬髋过渡';root.add(m);sockets.push(m);
 }
 const tail=new T.Group();tail.name=index?'金毛羽状尾':'哈士奇卷尾';root.add(tail);
 for(let k=0;k<10;k++){
  const m=new T.Mesh(new T.SphereGeometry(1,10,8),new T.MeshStandardMaterial({color:k>6?cream:coat,roughness:1}));
  tail.add(m);
 }
 return{surface,tufts,tail,sockets,index,coat,cream};
}
export function updateAnimalBody(T,b,Q,up,side,front,shoulder,hip,torso,radius){
 const pos=b.surface.geometry.attributes.position,col=b.surface.geometry.attributes.color;
 // Fixed shape in an orthonormal pose frame; shoulder chord is not body size.
 up=up.clone().normalize();side=side.clone().addScaledVector(up,-side.dot(up)).normalize();front=new T.Vector3().crossVectors(side,up).normalize();
 const center=Q[8].clone().add(Q[0]).multiplyScalar(.5);
 const point=(r,angle)=>{
  const width=shoulder*W[r],depth=width*DEPTH[r];
  return center.clone().addScaledVector(up,torso*(.5-Y[r])).addScaledVector(side,width*Math.cos(angle)).addScaledVector(front,depth*Math.sin(angle));
 };
 const color=(r,angle)=>{
  // Continuous breed pigmentation follows torso orientation, not camera.
  const f=Math.sin(angle),bib=Math.max(0,Math.min(1,(f-(b.index?-.05:.04))*2.6));
  const belly=(r>=4&&r<=8)?1:.75;
  return b.coat.clone().lerp(b.cream,bib*belly*(b.index?.65:1));
 };
 for(let r=0;r<RINGS;r++)for(let j=0;j<SIDES;j++){
  const a=j/SIDES*Math.PI*2,p=point(r,a),c=color(r,a),k=r*SIDES+j;
  pos.setXYZ(k,p.x,p.y,p.z);col.setXYZ(k,c.r,c.g,c.b);
 }
 pos.needsUpdate=true;col.needsUpdate=true;b.surface.geometry.computeVertexNormals();b.surface.geometry.computeBoundingSphere();
 const hp=b.tufts.geometry.attributes.position,hc=b.tufts.geometry.attributes.color;
 for(let k=0;k<960;k++){
  const r=1+k%9,a=((k*.61803398875)%1)*Math.PI*2;
  const radial=side.clone().multiplyScalar(Math.cos(a)).addScaledVector(front,Math.sin(a)).normalize();
  const direction=up.clone().multiplyScalar(-.78).addScaledVector(radial,.62).normalize();
  const p=point(r,a).addScaledVector(up,torso*((k*.41421356)%1-.5)*.09).addScaledVector(radial,radius*.04);
  const q=p.clone().addScaledVector(direction,radius*(b.index?.48:.36)),c=color(r,a);
  hp.setXYZ(k*2,p.x,p.y,p.z);hp.setXYZ(k*2+1,q.x,q.y,q.z);hc.setXYZ(k*2,c.r,c.g,c.b);hc.setXYZ(k*2+1,c.r,c.g,c.b);
 }
 hp.needsUpdate=true;hc.needsUpdate=true;
 for(let k=0;k<4;k++){
  const target=Q[[14,11,1,4][k]],base=(k<2?Q[8]:Q[0]).clone(),m=b.sockets[k];
  const dir=target.clone().sub(base),length=dir.length();m.position.copy(base.add(target).multiplyScalar(.5));m.quaternion.setFromUnitVectors(new T.Vector3(0,1,0),dir.normalize());m.scale.set(radius*(k<2?1.4:1.7),length*.56,radius*(k<2?1.7:1.8));
 }
 // The tail's shape is fixed in the moving torso basis. No independent
 // animation is added; it therefore cannot become a third motion source.
 const base=Q[0].clone().addScaledVector(front,-shoulder*.24).addScaledVector(up,-.015);
 for(let k=0;k<10;k++){
  const t=k/9,back=b.index?(Math.sin(t*Math.PI*.7)*.22):(Math.sin(t*Math.PI)*.19);
  const height=b.index?(-t*.36):(Math.sin(t*Math.PI*.65)*.30);
  const p=base.clone().addScaledVector(front,-back).addScaledVector(up,height).addScaledVector(side,.05*t);
  const m=b.tail.children[k],r=radius*(b.index?1.32:1.62)*(1-t*.70);
  m.position.copy(p);m.scale.set(r,r*1.55,r);m.quaternion.setFromUnitVectors(new T.Vector3(0,1,0),up);
 }
}
