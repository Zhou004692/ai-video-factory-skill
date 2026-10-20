"""Fixed-shape FK that retains input 3D directions, without depth sign guesses.

This is a geometric retargeting primitive, not world-motion/contact recovery.
"""
from __future__ import annotations
import math

H36M_PARENTS=(-1,0,1,2,0,4,5,0,7,8,9,8,11,12,8,14,15)


def preserve_pinhole_projection(point, focal_px, target_z):
    """Move an anchor along its view ray without changing its image centre.

    Contract: camera at (0,0,focal_px), looking down -Z with a fixed pinhole
    projection. Downstream common image homographies preserve the result.
    It does not infer head depth, scale, face expression or camera calibration.
    """
    if len(point)!=3 or not all(math.isfinite(v) for v in [*point,focal_px,target_z]) or focal_px<=0:
        raise ValueError('Finite xyz/depth and positive focal length required')
    old_depth=focal_px-point[2];new_depth=focal_px-target_z
    if min(old_depth,new_depth)<=0:raise ValueError('Anchor must remain in front of camera')
    ratio=new_depth/old_depth
    return [point[0]*ratio,point[1]*ratio,float(target_z)]


def bounded_angle_guide(radians, max_step):
    """Unwrap a render-only angle guide and bound per-frame angular change.

    This retains the existing head/paw continuity capability. It is NOT a
    source-pose measurement or an occlusion solution. Caller supplies its
    frame-rate-specific bound; raw input remains untouched.
    """
    if not radians or not math.isfinite(max_step) or max_step<=0 or any(not math.isfinite(v) for v in radians):
        raise ValueError('Finite nonempty angles and positive max_step required')
    unwrapped=[float(radians[0])]
    for a,b in zip(radians,radians[1:]):
        unwrapped.append(unwrapped[-1]+math.atan2(math.sin(b-a),math.cos(b-a)))
    result=[]
    for n in range(len(unwrapped)):
        lower=max(v-max_step*abs(k-n) for k,v in enumerate(unwrapped))
        upper=min(v+max_step*abs(k-n) for k,v in enumerate(unwrapped))
        result.append((lower+upper)/2)
    return result

def fixed_shape_fk(pose,lengths,parents=H36M_PARENTS):
    if len(pose)!=len(parents) or len(lengths)!=len(parents)-1:
        raise ValueError('Pose, topology and fixed lengths disagree')
    if parents[0]!=-1 or any(p<0 or p>=j for j,p in enumerate(parents[1:],1)):
        raise ValueError('Topology must be rooted and topologically ordered')
    if any(len(p)!=3 or not all(math.isfinite(v) for v in p) for p in pose):
        raise ValueError('Finite xyz required')
    if any(not math.isfinite(v) or v<=0 for v in lengths):raise ValueError('Positive fixed lengths required')
    out=[list(pose[0])]
    for j,parent in enumerate(parents[1:],1):
        vector=[pose[j][k]-pose[parent][k] for k in range(3)]
        length=math.sqrt(sum(v*v for v in vector))
        if length<1e-9:raise ValueError(f'degenerate source segment {parent}-{j}; estimate explicitly upstream')
        out.append([out[parent][k]+lengths[j-1]*vector[k]/length for k in range(3)])
    return out


def two_bone_ik(root, mid, tip, target, hint):
    """Analytic two-link target + bend-hint constraint, without stretching.

    Same target/hint contract described in Unity's Two Bone IK documentation;
    this independent law-of-cosines implementation does not use Unity code.
    It solves a supplied target, NOT floor calibration or contact detection.
    Unreachable targets return a length-preserving closest tip and residual.
    """
    if any(len(p)!=3 or not all(math.isfinite(v) for v in p) for p in (root,mid,tip,target,hint)):
        raise ValueError('Finite xyz points required')
    sub=lambda a,b:[a[k]-b[k] for k in range(3)]
    dot=lambda a,b:sum(x*y for x,y in zip(a,b))
    norm=lambda a:math.sqrt(dot(a,a))
    l1,l2=math.dist(root,mid),math.dist(mid,tip)
    if min(l1,l2)<1e-9:raise ValueError('Degenerate two-bone chain')
    delta=sub(target,root);distance=norm(delta)
    if distance<1e-9:
        delta=sub(tip,root)
        if norm(delta)<1e-9:delta=[0.,-1.,0.]
    axis=[v/norm(delta) for v in delta]
    length=max(abs(l1-l2)+1e-9,min(l1+l2-1e-9,distance))
    along=(l1*l1-l2*l2+length*length)/(2*length)
    height=math.sqrt(max(0.,l1*l1-along*along))
    pole=sub(hint,root);pole=[pole[k]-axis[k]*dot(pole,axis) for k in range(3)]
    if norm(pole)<1e-9:
        pole=sub(mid,root);pole=[pole[k]-axis[k]*dot(pole,axis) for k in range(3)]
    if norm(pole)<1e-9:
        least=min(range(3),key=lambda k:abs(axis[k]));pole=[float(k==least)-axis[k]*axis[least] for k in range(3)]
    pole=[v/norm(pole) for v in pole]
    newmid=[root[k]+axis[k]*along+pole[k]*height for k in range(3)]
    newtip=[root[k]+axis[k]*length for k in range(3)]
    error=math.dist(newtip,target)
    return {'mid':newmid,'tip':newtip,'reachable':error<1e-7,'target_error':error,
            'lengths':[l1,l2],'contact_verified':False}
