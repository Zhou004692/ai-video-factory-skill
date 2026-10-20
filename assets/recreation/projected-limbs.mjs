// Image-plane fixed-shape IK. No source-shot data or physical-depth inference.
export function solveProjectedChain(root,hint,target,upper,lower){
  if(![root,hint,target].every(p=>Array.isArray(p)&&p.length===3&&p.every(Number.isFinite))||
     ![upper,lower].every(n=>Number.isFinite(n)&&n>=.001))throw Error('Finite image-plane points and fixed lengths >= mesh minimum .001 required');
  const dx=target[0]-root[0],dy=target[1]-root[1],requested=Math.hypot(dx,dy);
  const distance=Math.max(Math.abs(upper-lower),Math.min(upper+lower,requested));
  let ux=dx,uy=dy;
  if(requested<1e-10){
    ux=hint[0]-root[0];uy=hint[1]-root[1];
    // A longer lower link folds the tip opposite the observed joint hint.
    // Reversing this axis keeps the same closest-tip residual while retaining
    // the visible elbow/knee branch instead of reflecting it across the root.
    if(lower>upper){ux=-ux;uy=-uy;}
  }
  const norm=Math.hypot(ux,uy);
  if(norm<1e-10)return {available:false,reason:'degenerate projected limb direction'};
  ux/=norm;uy/=norm;
  let joint,tip,bendResolved=true;
  if(distance<1e-10){
    joint=[root[0]+upper*ux,root[1]+upper*uy,0];tip=[...root];
  }else{
    const along=(upper*upper-lower*lower+distance*distance)/(2*distance);
    const height=Math.sqrt(Math.max(0,upper*upper-along*along));
    const cross=(hint[0]-root[0])*(-uy)+(hint[1]-root[1])*ux;
    bendResolved=Math.abs(cross)>1e-9||height<1e-7;
    if(!bendResolved)return {available:false,reason:'fixed shape requires a bend but observed 2D hint is collinear; branch unknown'};
    const sign=cross<0?-1:1;
    joint=[root[0]+along*ux-sign*height*uy,root[1]+along*uy+sign*height*ux,0];
    tip=[root[0]+distance*ux,root[1]+distance*uy,0];
  }
  return {available:true,root:[...root],joint,tip,requestedTarget:[...target],
    fixed_upper_length:upper,fixed_lower_length:lower,
    target_error_local:Math.hypot(tip[0]-target[0],tip[1]-target[1]),
    reach_limited:Math.abs(distance-requested)>1e-7,bend_hint_resolved:bendResolved,
    coordinate_domain:'screen-space image plane; z=0 is staging, not inferred physical depth'};
}
