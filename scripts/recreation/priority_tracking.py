"""Optional NumPy/SciPy local limb tracking with explicit hard constraints.

Protected image motion is not traded against smoothing in a weighted sum.
This is a local constrained solve, not global IK, SoT, or 3D ground truth.
"""
from __future__ import annotations


def ray_sphere_intersections(origin,direction,center,radius):
    """Stdlib analytic geometry audit; never extend a ray or enlarge a bone.

    Positive intersections of a camera ray with the fixed-length sphere around
    a parent joint. Two solutions remain depth-ambiguous, not ground truth.
    """
    import math
    try:
        o=[float(x) for x in origin];d=[float(x) for x in direction];c=[float(x) for x in center];r=float(radius)
    except (TypeError,ValueError) as exc:raise ValueError('Finite 3D ray/sphere required') from exc
    if len(o)!=3 or len(d)!=3 or len(c)!=3 or not all(math.isfinite(x) for x in o+d+c+[r]) or r<=0:raise ValueError('Finite 3D ray and positive radius required')
    norm=math.sqrt(math.fsum(x*x for x in d))
    if norm<1e-15:raise ValueError('Nonzero ray direction required')
    d=[x/norm for x in d];relative=[c[i]-o[i] for i in range(3)];distance=math.fsum(relative[i]*d[i] for i in range(3))
    perpendicular=[relative[i]-distance*d[i] for i in range(3)];closest2=math.fsum(x*x for x in perpendicular);margin=r*r-closest2
    tolerance=1e-12*max(1.,r*r);line_hits=margin>=-tolerance;points=[];ray_distances=[]
    if line_hits:
        delta=math.sqrt(max(0.,margin));values=[distance] if delta<=1e-12 else [distance-delta,distance+delta]
        for value in values:
            if value>=0:points.append([o[i]+value*d[i] for i in range(3)]);ray_distances.append(value)
    return dict(points=points,ray_distances=ray_distances,line_intersects_sphere=line_hits,
                line_closest_distance=math.sqrt(closest2),sphere_radius=r,
                squared_intersection_margin=margin,source_3d_truth_recovered=False)


def max_parent_ray_depth(hip_to_knee_ray_angle,knee_to_ankle_ray_angle,thigh_length,shin_length):
    """Conditional upper bound for acute camera-ray angles, in relative units.

    Along the knee ray, ankle-ray intersection needs knee depth <= Lshin/sin(b).
    Maximize l*cos(a)+sqrt(Lthigh**2-l**2*sin(a)**2) over that interval.
    Smaller a,b give an optimistic upper bound for widened pixel cones; this
    does not calibrate the camera or recover a unique parent trajectory.
    """
    import math
    a,b,L1,L2=map(float,[hip_to_knee_ray_angle,knee_to_ankle_ray_angle,thigh_length,shin_length])
    if not all(math.isfinite(x) for x in [a,b,L1,L2]) or not 0<=a<math.pi/2 or not 0<=b<math.pi/2 or min(L1,L2)<=0:raise ValueError('Acute nonnegative ray angles and positive fixed lengths required')
    max_knee=math.inf if b<=1e-12 else L2/math.sin(b)
    if a<=1e-12:return L1+max_knee
    knee=min(max_knee,L1*math.cos(a)/math.sin(a))
    return knee*math.cos(a)+math.sqrt(max(0.,L1*L1-knee*knee*math.sin(a)**2))


def chain_reach_bound(origin,center_ray,base,total_length,max_ray_angle_rad):
    """Necessary reach condition for a fixed base and an enclosing image cone.

    Triangle inequality bounds endpoint distance by the sum of fixed bones.
    Distance to an enlarged cone is a LOWER bound on distance to the pixel
    disk's rays. A conflict is conditional on the supplied camera/base/shape;
    absence of a conflict does not establish feasibility or recover 3D truth.
    """
    import math
    o=list(map(float,origin));d=list(map(float,center_ray));b=list(map(float,base))
    L=float(total_length);epsilon=float(max_ray_angle_rad)
    if len(o)!=3 or len(d)!=3 or len(b)!=3 or not all(math.isfinite(x) for x in o+d+b+[L,epsilon]) or L<=0 or not 0<=epsilon<=math.pi:raise ValueError('Finite 3D geometry, positive length and cone angle in [0,pi] required')
    dn=math.sqrt(math.fsum(x*x for x in d))
    if dn<1e-15:raise ValueError('Nonzero ray direction required')
    v=[b[i]-o[i] for i in range(3)];distance=math.sqrt(math.fsum(x*x for x in v))
    angle=math.acos(max(-1.,min(1.,math.fsum(v[i]*d[i] for i in range(3))/(distance*dn)))) if distance>1e-15 else 0.
    lower=distance*math.sin(min(math.pi/2,max(0.,angle-epsilon)))
    return dict(distance_to_enclosing_cone=lower,total_fixed_length=L,
                fixed_base_reach_conflict=lower>L+1e-9*max(1.,L,distance),
                minimum_base_displacement_lower_bound=max(0.,lower-L),
                conditional_on_supplied_geometry=True,source_3d_truth_recovered=False)


def protect_limb_window(prior_xyz,target_ankle_uv,track_mask,camera_sequence,*,
                        freeze,foot_surface_offsets_world,source_contact,
                        floor_normal,floor_constant,body_scale,
                        ankle_tolerance_px=3.,knee_uv=None,knee_tolerance_px=None,
                        knee_mask=None,floor_tolerance_world=.0035,
                        adjacent_angle_limits_deg=None,
                        max_rotation_delta_rad=.35,max_hinge_delta_rad=.35,
                        motion_accel_sigma_units=.03,pose_prior_sigma_units=.3,
                        maxiter=100):
    """Move a two-bone limb, freezing its base, body and external context.

    prior_xyz is T x (base,knee,ankle) x xyz with fixed lengths. Supplied foot
    surface offsets are world-space mesh vertices relative to the ankle; their
    orientation is frozen. True source_contact only constrains plane-normal
    height, never a fixed XYZ. Caller track_mask is explicit reviewed evidence.
    Only candidates passing fresh constraints AND solver status are usable.
    """
    import numpy as np
    from scipy.optimize import minimize
    from scipy.spatial.transform import Rotation
    q0=np.array(prior_xyz,float,copy=True);target=np.array(target_ankle_uv,float,copy=True)
    if q0.ndim!=3 or q0.shape[1:]!=(3,3) or len(q0)<4:raise ValueError('At least four base/knee/ankle frames required')
    T=len(q0);mask=np.asarray(track_mask,bool);frozen=np.asarray(freeze,bool);contact=np.asarray(source_contact,bool)
    offsets=np.array(foot_surface_offsets_world,float,copy=True)
    try:
        R=np.array(camera_sequence['rotation_world_to_cv'],float,copy=True);tv=np.array(camera_sequence['translation_world_to_cv'],float,copy=True)
        K=np.array(camera_sequence['intrinsics_px'],float,copy=True)
    except (KeyError,TypeError,ValueError) as exc:raise ValueError('Explicit camera sequence required') from exc
    if target.shape!=(T,2) or mask.shape!=(T,) or frozen.shape!=(T,) or contact.shape!=(T,) or not mask.any():raise ValueError('Explicit aligned tracking and support masks required')
    if offsets.ndim!=3 or offsets.shape[0]!=T or offsets.shape[2]!=3 or offsets.shape[1]<1:raise ValueError('Aligned explicit foot surface vertices required')
    if R.shape!=(T,3,3) or tv.shape!=(T,3) or K.shape!=(T,3,3):raise ValueError('Explicit camera arrays must align with all frames')
    scalars=[body_scale,ankle_tolerance_px,floor_tolerance_world,max_rotation_delta_rad,max_hinge_delta_rad,motion_accel_sigma_units,pose_prior_sigma_units]
    if not all(np.isfinite(a).all() for a in [q0,target,offsets,R,tv,K,floor_normal]) or not all(np.isfinite(x) and x>0 for x in scalars) or not np.isfinite(floor_constant):raise ValueError('Finite inputs and positive scales required')
    if not np.allclose(R@R.transpose(0,2,1),np.eye(3),atol=1e-7,rtol=0) or not np.allclose(np.linalg.det(R),1.,atol=1e-7,rtol=0):raise ValueError('Proper world-to-camera rotation required')
    if (K[:,0,0]<=0).any() or (K[:,1,1]<=0).any() or not np.allclose(K[:,2],[0,0,1],atol=1e-10,rtol=0) or not np.allclose(K[:,1,0],0.,atol=1e-10,rtol=0):raise ValueError('Positive pinhole intrinsics required')
    N=np.array(floor_normal,float);norm=np.linalg.norm(N)
    if N.shape!=(3,) or norm<1e-9:raise ValueError('Nonzero floor normal required')
    N/=norm;height=floor_constant/norm;S=float(body_scale)
    limits=None if adjacent_angle_limits_deg is None else np.array(adjacent_angle_limits_deg,float,copy=True)
    if limits is not None and (limits.shape!=(T-1,2) or not np.isfinite(limits).all() or (limits<0).any() or (limits>180).any()):raise ValueError('Explicit per-frame-pair two-bone angle limits in [0,180] required')
    if knee_uv is None:
        if knee_tolerance_px is not None or knee_mask is not None:raise ValueError('Knee constraints require targets and explicit tolerances')
        ku=np.zeros((T,2));kt=np.ones(T);km=np.zeros(T,bool)
    else:
        ku=np.array(knee_uv,float,copy=True);kt=np.array(knee_tolerance_px,float,copy=True);km=np.ones(T,bool) if knee_mask is None else np.asarray(knee_mask,bool)
        if ku.shape!=(T,2) or kt.shape!=(T,) or km.shape!=(T,) or not np.isfinite(ku).all() or not np.isfinite(kt).all() or (kt<=0).any():raise ValueError('Aligned finite knee targets, positive tolerances and masks required')
    v=q0[:,1]-q0[:,0];w=q0[:,2]-q0[:,1];lens=np.stack([np.linalg.norm(v,axis=1),np.linalg.norm(w,axis=1)],axis=1);L=np.median(lens,axis=0)
    if (L<1e-8).any() or (np.ptp(lens,axis=0)/L>1e-6).any():raise ValueError('Fixed nonzero limb lengths required')
    reach_audits=[]
    for n in np.flatnonzero(mask):
        # For pinhole K, angular ray change along a pixel segment is at most
        # its length / sigma_min(K[:2,:2]); the unnormalized ray has z=1.
        # This also covers skew, and enlarges rather than shrinks the disk.
        center=-(R[n].T@tv[n]);ray=R[n].T@np.linalg.solve(K[n],np.r_[target[n],1.])
        focal_min=float(np.linalg.svd(K[n,:2,:2],compute_uv=False).min())
        audit=chain_reach_bound(center,ray,q0[n,0],float(L.sum()),min(np.pi,ankle_tolerance_px/focal_min))
        reach_audits.append(dict(frame_index=int(n),**audit))
    reach_conflicts=[a['frame_index'] for a in reach_audits if a['fixed_base_reach_conflict']]
    frames=[];angles=[];fallback=[]
    for n in range(T):
        y=-v[n]/lens[n,0];shin=w[n]/lens[n,1];z=shin-y*np.dot(shin,y)
        beta=float(np.arccos(np.clip(np.dot(-y,shin),-1,1)))
        if beta>=np.pi-1e-6:raise ValueError('Fully folded ambiguous limb needs an explicit reviewed branch')
        if np.linalg.norm(z)<1e-7:
            fallback.append(n);hint=frames[-1][:,2] if frames else np.array([0.,0.,1.]);z=hint-y*np.dot(hint,y)
            if np.linalg.norm(z)<1e-7:
                hint=np.eye(3)[np.argmin(abs(y))];z=hint-y*np.dot(hint,y)
        z/=np.linalg.norm(z);x=np.cross(y,z);x/=np.linalg.norm(x);z=np.cross(x,y)
        frames.append(np.column_stack([x,y,z]));angles.append(beta)
    frames=np.array(frames);angles=np.array(angles);free=np.flatnonzero(~frozen)
    if not len(free):raise ValueError('At least one editable frame required')

    def decode(x):
        delta=x.reshape(-1,4);rot=Rotation.from_rotvec(delta[:,:3]).as_matrix()@frames[free];beta=angles[free]+delta[:,3]
        q=q0.copy();q[free,1]=q0[free,0]+rot@np.array([0.,-L[0],0.])
        q[free,2]=q[free,1]+np.einsum('tij,tj->ti',rot,np.c_[np.zeros(len(free)),-L[1]*np.cos(beta),L[1]*np.sin(beta)])
        return q

    def project(q):
        cv=np.einsum('tij,tkj->tki',R,q)+tv[:,None,:];h=np.einsum('tij,tkj->tki',K,cv)
        return h[...,:2]/np.maximum(cv[...,2,None],.01),cv[...,2]

    def checks(q):
        image,depth=project(q);gap=(q[:,2,None]+offsets)@N-height;gap=gap.min(axis=1)
        ae=np.linalg.norm(image[:,2]-target,axis=1);ke=np.linalg.norm(image[:,1]-ku,axis=1)
        return image,depth,gap,ae,ke

    def constraints(x):
        q=decode(x);_,depth,gap,ae,ke=checks(q)
        parts=[1.-(ae[mask]/ankle_tolerance_px)**2,1.-(ke[km]/kt[km])**2,
            (gap+floor_tolerance_world)/S,(floor_tolerance_world-gap[contact])/S,((depth-.01)/S).ravel()]
        if limits is not None:
            vectors=np.diff(q,axis=1);vectors/=np.linalg.norm(vectors,axis=2,keepdims=True)
            parts.append((np.sum(vectors[1:]*vectors[:-1],axis=2)-np.cos(np.deg2rad(limits))).ravel())
        return np.concatenate(parts)

    def objective(x):
        q=decode(x)
        return float((np.sum(((q-q0)/(S*pose_prior_sigma_units))**2)+
                      np.sum((np.diff(q[:,1:],n=2,axis=0)/(S*motion_accel_sigma_units))**2)+
                      np.sum((x.reshape(-1,4)/max_rotation_delta_rad)**2))/T)

    x0=np.zeros(len(free)*4);_,_,initial_gap,initial_ae,initial_ke=checks(q0)
    conflict=bool((initial_ae[frozen&mask]>ankle_tolerance_px+1e-6).any() or (initial_ke[frozen&km]>kt[frozen&km]+1e-6).any() or
        (initial_gap[frozen]<-floor_tolerance_world-1e-6).any() or (initial_gap[frozen&contact]>floor_tolerance_world+1e-6).any())
    if conflict:
        q=q0.copy();success=False;message='Frozen context violates explicit tracking or surface constraints';calls=0;iterations=0
    elif reach_conflicts:
        q=q0.copy();success=False;message='Fixed base and bone lengths cannot reach the enclosing target image cone under the supplied camera';calls=0;iterations=0
    else:
        lo=np.full((len(free),4),-max_rotation_delta_rad);hi=-lo
        lo[:,3]=np.maximum(-max_hinge_delta_rad,-angles[free]);hi[:,3]=np.minimum(max_hinge_delta_rad,np.pi-1e-6-angles[free])
        fit=minimize(objective,x0,method='SLSQP',bounds=list(zip(lo.ravel(),hi.ravel())),constraints=[dict(type='ineq',fun=constraints)],options=dict(maxiter=maxiter,ftol=1e-9,disp=False))
        q=decode(fit.x);success=bool(fit.success);message=str(fit.message);calls=1;iterations=int(fit.nit)
    _,depth,gap,ae,ke=checks(q)
    tracking_pass=bool((ae[mask]<=ankle_tolerance_px+1e-5).all());knee_pass=bool((ke[km]<=kt[km]+1e-5).all())
    surface_pass=bool((gap>=-floor_tolerance_world-1e-6).all() and (gap[contact]<=floor_tolerance_world+1e-6).all())
    vectors=np.diff(q,axis=1);vectors/=np.linalg.norm(vectors,axis=2,keepdims=True)
    angle_changes=np.rad2deg(np.arccos(np.clip(np.sum(vectors[1:]*vectors[:-1],axis=2),-1,1)))
    angle_pass=limits is None or bool((angle_changes<=limits+1e-4).all())
    bone_error=float(abs(np.stack([np.linalg.norm(q[:,1]-q[:,0],axis=1),np.linalg.norm(q[:,2]-q[:,1],axis=1)],axis=1)-L).max())
    diagnostics=dict(optimizer_success=success,optimizer_message=message,iterations=iterations,solver_calls=calls,
        protected_ankle_max_error_px=float(ae[mask].max()),ankle_tolerance_px=ankle_tolerance_px,tracking_constraint_pass=tracking_pass,
        knee_constraint_pass=knee_pass,surface_constraint_pass=surface_pass,adjacent_angle_constraint_pass=angle_pass,bone_length_max_error_world=bone_error,
        frozen_context_identical=bool(np.array_equal(q[frozen],q0[frozen])),base_motion_identical=bool(np.array_equal(q[:,0],q0[:,0])),
        candidate_usable=success and tracking_pass and knee_pass and surface_pass and angle_pass and bool((depth>.01-1e-6).all()),
        source_3d_truth_recovered=False,static_position_locks=0,straight_limb_pole_fallback_frames=fallback,
        fixed_base_reach_conflict_frames=reach_conflicts,reach_audits=reach_audits)
    return dict(xyz=q,diagnostics=diagnostics,ankle_errors_px=ae,surface_min_heights=gap)
