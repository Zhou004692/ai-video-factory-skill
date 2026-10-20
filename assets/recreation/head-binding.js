export function fixedCervicalPose(T,{base,up,headSize,neckLength,skullMountRatio,yaw}){
 if(!(headSize>0&&neckLength>0)||!Number.isFinite(yaw)||up.lengthSq()<1e-12)throw Error('Explicit finite cervical inputs required');
 const y=up.clone().normalize(),z=new T.Vector3(Math.sin(yaw),0,Math.cos(yaw));z.addScaledVector(y,-z.dot(y));if(z.lengthSq()<1e-12)throw Error('Degenerate head up/forward');z.normalize();const x=new T.Vector3().crossVectors(y,z).normalize();z.crossVectors(x,y).normalize();
 const rotation=new T.Quaternion().setFromRotationMatrix(new T.Matrix4().makeBasis(x,y,z)).normalize();const mount=base.clone().addScaledVector(y,neckLength),offset=new T.Vector3(...skullMountRatio).multiplyScalar(headSize).applyQuaternion(rotation);
 return{position:mount.clone().sub(offset),rotation,mount};
}