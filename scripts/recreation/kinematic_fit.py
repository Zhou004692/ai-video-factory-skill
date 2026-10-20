"""Optional NumPy/SciPy fit-stage primitive, independent of models and services.

Joint projection, motion prior and contact terms follow the established fitting
pattern documented by HuMoR. This is an independent fixed-bone solver, NOT
HuMoR, SMPL, a learned motion prior or a source-ground-truth reconstruction.
"""
from __future__ import annotations


def audit_support(gap_by_frame,source_states,*,gap_tolerance=.03):
    """Audit ALL frames, including those with no predicted contact candidate.

    Gaps must be independently measured on actual meshes in body units.
    Grounded/flight labels are supplied evidence; this function infers none.
    Unknown airborne frames are unresolved, never silently accepted as jumps.
    """
    import math
    if len(gap_by_frame)!=len(source_states) or not gap_by_frame or not math.isfinite(gap_tolerance) or gap_tolerance<=0:
        raise ValueError('Aligned nonempty frames and positive tolerance required')
    known_float=[];unknown_float=[];penetration=[];flight=[];unknown=[]
    for n,(gaps,state) in enumerate(zip(gap_by_frame,source_states)):
        if state not in ['grounded','flight','unknown'] or not gaps or not all(math.isfinite(g) for g in gaps):raise ValueError('Finite foot gaps and explicit source states required')
        if min(gaps)<-gap_tolerance:penetration.append(n)
        if state=='unknown':unknown.append(n)
        if state=='flight':flight.append(n)
        elif min(gaps)>gap_tolerance:
            (known_float if state=='grounded' else unknown_float).append(n)
    geometry_passed=not(known_float or unknown_float or penetration)
    return {'grounded_float_frames':known_float,'unresolved_float_frames':unknown_float,'penetration_frames':penetration,
            'source_flight_frames':flight,'unknown_state_frames':unknown,'geometry_passed':geometry_passed,
            'accepted':geometry_passed and not unknown,'labels_inferred':False}


def fit_window(prior_world,parents,observed_uv,observation_weights,*,mutable_bones,
               body_scale,focal_px,floor_normal,floor_constant,foot_joints,
               sole_offsets,contact_weights,freeze,image_size=(1080,610),
               projection_sigma_px=8.,contact_sigma_units=.003,
               prior_sigma_units=.3,correction_acceleration_sigma_units=.03,
               max_root_shift_units=4.,max_nfev=100,fit_root=True,temporal_mode='correction',
               contact_targets_world=None,contact_position_weights=None,
               contact_position_offsets=None,contact_position_sigma_units=.01,
               camera_sequence=None):
    """Fit a window without retiming. Contact evidence is supplied by the caller.

    Position targets constrain a caller-selected FIXED material point for each
    contact episode. Position offsets may differ from minimum-height offsets;
    neither low height nor a supplied target silently enables a static lock.

    Optional camera_sequence supplies explicit (T,3,3) rotation_world_to_cv,
    (T,3) translation_world_to_cv and (T,3,3) intrinsics_px. CV axes are right,
    down, forward; points obey K @ (R @ world + t), with positive depth. Cameras
    remain fixed during fitting; this does not calibrate or validate the scene.
    With no sequence the historical fixed-camera projection is unchanged.
    """
    import numpy as np
    from scipy import sparse
    from scipy.optimize import least_squares
    if temporal_mode not in ['correction','motion']:raise ValueError('Temporal mode must be explicit: correction or motion')
    q0=np.array(prior_world,float,copy=True);uv=np.array(observed_uv,float,copy=True)
    weights=np.array(observation_weights,float,copy=True);parents=tuple(parents)
    if q0.ndim!=3 or q0.shape[-1]!=3:raise ValueError('Expected time x joints x xyz')
    T,J,_=q0.shape;mutable=tuple(mutable_bones);feet=tuple(foot_joints);K=len(mutable);F=len(feet)
    off=np.array(sole_offsets,float,copy=True);cw=np.array(contact_weights,float,copy=True);frozen=np.asarray(freeze,bool)
    if T<4 or J!=len(parents) or parents[0]!=-1 or any(not 0<=p<j for j,p in enumerate(parents[1:],1)):
        raise ValueError('Need >=4 frames and topologically ordered rooted skeleton')
    if len(set(mutable))!=K or any(not 1<=j<J for j in mutable) or any(not 0<=j<J for j in feet):raise ValueError('Invalid mutable/contact joints')
    if uv.shape!=(T,J,2) or weights.shape!=(T,J) or off.shape!=(T,F,3) or cw.shape!=(T,F) or frozen.shape!=(T,):raise ValueError('Input arrays do not align')
    if camera_sequence is not None:
        try:
            camera_R=np.array(camera_sequence['rotation_world_to_cv'],float,copy=True)
            camera_t=np.array(camera_sequence['translation_world_to_cv'],float,copy=True)
            camera_K=np.array(camera_sequence['intrinsics_px'],float,copy=True)
        except (KeyError,TypeError,ValueError) as exc:
            raise ValueError('Explicit aligned camera rotation, translation and intrinsics required') from exc
        if camera_R.shape!=(T,3,3) or camera_t.shape!=(T,3) or camera_K.shape!=(T,3,3):
            raise ValueError('Camera arrays must explicitly align with every input frame')
        if not all(np.isfinite(a).all() for a in [camera_R,camera_t,camera_K]):raise ValueError('Finite camera arrays required')
        if not np.allclose(camera_R@camera_R.transpose(0,2,1),np.eye(3),atol=1e-7,rtol=0) or not np.allclose(np.linalg.det(camera_R),1.,atol=1e-7,rtol=0):
            raise ValueError('World-to-CV rotations must be proper orthonormal matrices')
        if (camera_K[:,0,0]<=0).any() or (camera_K[:,1,1]<=0).any() or not np.allclose(camera_K[:,2],[0,0,1],atol=1e-10,rtol=0) or not np.allclose(camera_K[:,1,0],0.,atol=1e-10,rtol=0):
            raise ValueError('Positive focal lengths and pinhole intrinsics required')
    if (contact_targets_world is None)!=(contact_position_weights is None):raise ValueError('Contact position targets require explicit aligned weights')
    if contact_targets_world is None:
        if contact_position_offsets is not None:raise ValueError('Position offsets require explicit contact targets and weights')
        targets=np.zeros((T,F,3));pw=np.zeros((T,F));poff=off
    else:
        targets=np.array(contact_targets_world,float,copy=True);pw=np.array(contact_position_weights,float,copy=True)
        poff=off if contact_position_offsets is None else np.array(contact_position_offsets,float,copy=True)
        if targets.shape!=(T,F,3) or pw.shape!=(T,F) or poff.shape!=(T,F,3):raise ValueError('Contact position arrays do not align')
    arrays=[q0,uv,weights,off,cw,targets,pw,poff,np.asarray(floor_normal),np.asarray(image_size)]
    scales=[body_scale,focal_px,projection_sigma_px,contact_sigma_units,contact_position_sigma_units,prior_sigma_units,correction_acceleration_sigma_units,max_root_shift_units]
    if not all(np.isfinite(a).all() for a in arrays) or not all(np.isfinite(s) and s>0 for s in scales) or not np.isfinite(floor_constant):raise ValueError('Finite inputs and positive scales required')
    if any((a<0).any() or (a>1).any() for a in [weights,cw,pw]):raise ValueError('Explicit weights must be in [0,1]')
    N=np.array(floor_normal,float);norm=np.linalg.norm(N)
    if N.shape!=(3,) or norm<1e-9:raise ValueError('Nonzero floor normal required')
    N/=norm;height=floor_constant/norm;S=float(body_scale)
    vectors=q0[:,1:]-q0[:,parents[1:]];lens=np.linalg.norm(vectors,axis=-1);L=np.median(lens,axis=0)
    if (L<1e-8).any() or (np.ptp(lens,axis=0)/L>1e-6).any():raise ValueError('Prior must have fixed nondegenerate bone lengths')
    directions=vectors/lens[...,None];free=np.flatnonzero(~frozen);D=3+3*K
    if not len(free):raise ValueError('At least one unfrozen frame required')
    slots={j:k for k,j in enumerate(mutable)}

    def decode(x):
        state=np.zeros((T,D));state[free]=x.reshape(-1,D)
        if not fit_root:state[:,:3]=0
        raw=directions[:,np.array(mutable,dtype=int)-1]+state[:,3:].reshape(T,K,3)
        unit=raw/np.maximum(np.linalg.norm(raw,axis=-1,keepdims=True),1e-10)
        q=np.empty_like(q0);q[:,0]=q0[:,0]+state[:,:3]*S
        for j,p in enumerate(parents[1:],1):q[:,j]=q[:,p]+L[j-1]*(unit[:,slots[j]] if j in slots else directions[:,j-1])
        q[frozen]=q0[frozen]
        return q,raw

    def camera_points(q):
        if camera_sequence is None:
            return np.stack([q[...,0],-q[...,1],focal_px-q[...,2]],axis=-1)
        return np.einsum('tij,tkj->tki',camera_R,q)+camera_t[:,None,:]

    def project(q):
        if camera_sequence is None:
            depth=focal_px-q[...,2];den=np.maximum(depth,.01)
            return np.stack([image_size[0]/2+focal_px*q[...,0]/den,image_size[1]/2-focal_px*q[...,1]/den],axis=-1)
        points=camera_points(q);den=np.maximum(points[...,2],.01)
        pixels=np.einsum('tij,tkj->tki',camera_K,points)
        return pixels[...,:2]/den[...,None]

    def residual(x):
        q,raw=decode(x);gap=(q[:,feet]+off)@N-height
        corr=q-q0
        return np.concatenate([
            ((project(q)-uv)*np.sqrt(weights)[...,None]/projection_sigma_px).ravel(),
            (corr/(S*prior_sigma_units)).ravel(),
            ((np.linalg.norm(raw,axis=-1)-1)*.1).ravel(),
            (gap*np.sqrt(cw)/(S*contact_sigma_units)).ravel(),
            (np.minimum(gap,0)/(S*contact_sigma_units)).ravel(),
            ((q[:,feet]+poff-targets)*np.sqrt(pw)[...,None]/(S*contact_position_sigma_units)).ravel(),
            ((np.maximum(q[...,2]-focal_px+.01,0) if camera_sequence is None else np.maximum(.01-camera_points(q)[...,2],0))/S).ravel(),
            (np.diff(q if temporal_mode=='motion' else corr,n=2,axis=0)/(S*correction_acceleration_sigma_units)).ravel()])

    x0=np.zeros(len(free)*D);r0=residual(x0)
    pattern=sparse.lil_matrix((len(r0),len(x0)),dtype=np.int8);columns={n:slice(k*D,(k+1)*D) for k,n in enumerate(free)}
    start=0
    for size in [J*2,J*3,K,F,F,F*3,J]:
        for n in free:pattern[start+n*size:start+(n+1)*size,columns[n]]=1
        start+=T*size
    for n in range(T-2):
        for frame in range(n,n+3):
            if frame in columns:pattern[start+n*J*3:start+(n+1)*J*3,columns[frame]]=1
    lower=np.full((len(free),D),-2.);upper=-lower;lower[:,:3]=-max_root_shift_units;upper[:,:3]=max_root_shift_units
    result=least_squares(residual,x0,jac_sparsity=pattern.tocsr(),bounds=(lower.ravel(),upper.ravel()),max_nfev=max_nfev,
                         ftol=1e-7,xtol=1e-7,gtol=1e-7,verbose=0)
    q,_=decode(result.x);mask=weights>0
    before=np.linalg.norm(project(q0)-uv,axis=-1);after=np.linalg.norm(project(q)-uv,axis=-1)
    gap_before=((q0[:,feet]+off)@N-height)/S;gap_after=((q[:,feet]+off)@N-height)/S
    position_before=np.linalg.norm(q0[:,feet]+poff-targets,axis=-1)/S
    position_after=np.linalg.norm(q[:,feet]+poff-targets,axis=-1)/S
    diagnostics={'optimizer_success':bool(result.success),'optimizer_message':str(result.message),'function_evaluations':result.nfev,
      'cost_before':float(np.dot(r0,r0)/2),'cost_after':float(result.cost),
      'projection_rmse_before_px':float(np.sqrt(np.mean(before[mask]**2))) if mask.any() else None,
      'projection_rmse_after_px':float(np.sqrt(np.mean(after[mask]**2))) if mask.any() else None,
      'contact_gap_max_before_units':float(abs(gap_before[cw>0]).max()) if (cw>0).any() else None,
      'contact_gap_max_after_units':float(abs(gap_after[cw>0]).max()) if (cw>0).any() else None,
      'contact_position_max_before_units':float(position_before[pw>0].max()) if (pw>0).any() else None,
      'contact_position_max_after_units':float(position_after[pw>0].max()) if (pw>0).any() else None,
      'contact_position_active_samples':int((pw>0).sum()),
      'bone_length_max_error_world':float(abs(np.linalg.norm(q[:,1:]-q[:,parents[1:]],axis=-1)-L).max()),
      'max_root_shift_body_units':float(np.linalg.norm(q[:,0]-q0[:,0],axis=-1).max()/S),
      'projection_camera_mode':'legacy_fixed' if camera_sequence is None else 'explicit_world_to_cv',
      'camera_parameters_optimized':False,
      'camera_depth_min_world':float(camera_points(q)[...,2].min()),
      'all_joints_in_front_of_camera':bool((camera_points(q)[...,2]>.01).all()),
      'temporal_mode':temporal_mode,'source_3d_truth_recovered':False,'source_support_labels_verified_by_solver':False,'automatically_accepted':False}
    return {'xyz':q,'diagnostics':diagnostics}
