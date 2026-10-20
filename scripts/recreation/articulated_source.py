"""Optional NumPy-only source rig with rigid body clusters and hinge limbs.

This is authored kinematics, not a learned or metrically calibrated human model.
Original WholeBody133 observations must remain separate and retained by callers.
"""
from __future__ import annotations

PARENTS=(-1,0,1,2,0,4,5,0,7,8,9,8,11,12,8,14,15)
PELVIS=(0,1,4,7)
CHEST=(7,8,11,14)
LIMBS=((1,2,3),(4,5,6),(11,12,13),(14,15,16))


def build_source_rig(reference_xyz,foot_geometry):
    """Freeze the caller's source reference, not an animal or implicit T-pose.

    Foot dimensions are explicit authored proxy dimensions in the same relative
    units. They do not infer the source's shoe size or certify a ground plane.
    """
    import numpy as np
    q=np.array(reference_xyz,float,copy=True)
    if q.shape!=(17,3) or not np.isfinite(q).all():raise ValueError('Finite explicit source h36m17 reference required')
    lengths=np.linalg.norm(q[1:]-q[list(PARENTS[1:])],axis=1)
    if (lengths<1e-8).any():raise ValueError('Nondegenerate reference bones required')
    try:
        w,h,t,a=[float(foot_geometry[k]) for k in ['width','heel_length','toe_length','ankle_height']]
    except (KeyError,TypeError,ValueError) as exc:raise ValueError('Explicit width, heel/toe length and ankle height required') from exc
    if not np.isfinite([w,h,t,a]).all() or min(w,h,t,a)<=0:raise ValueError('Finite positive foot dimensions required')
    vertices=np.array([[x,y,z] for y in [-a,0.] for z in [-h,t] for x in [-w/2,w/2]])
    return dict(schema='recreation-articulated-source-v1',source_joint_schema='h36m17-derived-from-retained-wholebody133',
        reference_xyz=q.tolist(),parents=list(PARENTS),bone_lengths=lengths.tolist(),
        pelvis_local=(q[list(PELVIS)]-q[0]).tolist(),chest_local=(q[list(CHEST)]-q[7]).tolist(),
        foot_vertices_local=vertices.tolist(),foot_marker_local=[[w/3,-a,t],[-w/3,-a,t],[0.,-a,-h]],
        foot_marker_names=['positive_forefoot_support','negative_forefoot_support','heel_support'],
        foot_faces=[[0,1,3,2],[4,6,7,5],[0,4,5,1],[2,3,7,6],[0,2,6,4],[1,5,7,3]],
        foot_geometry_provenance='authored rigid proxy; not measured shoe or paw',
        metric_dimensions_verified=False,reference_is_measured_bind_pose=False)


def forward_source_rig(rig,*,root_world,pelvis_rotation,chest_rotation,limb_rotations,
                       hinge_angles,foot_rotations,cranial_directions):
    """Forward kinematics, hard rigid clusters, fixed lengths and no stretching.

    Pelvis rotation is world-space; chest is relative to pelvis. Limb rotations
    are relative to pelvis/chest; local -Y is the first bone and the second
    bends toward +Z. Foot rotations are world-space, attached at the ankles.
    Cranial directions are two nonzero chest-local neck/head directions.
    Hinge angles select a nonnegative branch, not a biomechanical certification.
    """
    import numpy as np
    if rig.get('schema')!='recreation-articulated-source-v1':raise ValueError('Explicit articulated source contract required')
    root=np.array(root_world,float);rp=np.array(pelvis_rotation,float);rc_local=np.array(chest_rotation,float)
    limb=np.array(limb_rotations,float);angle=np.array(hinge_angles,float);foot=np.array(foot_rotations,float);head=np.array(cranial_directions,float)
    if root.shape!=(3,) or rp.shape!=(3,3) or rc_local.shape!=(3,3) or limb.shape!=(4,3,3) or angle.shape!=(4,) or foot.shape!=(2,3,3) or head.shape!=(2,3):raise ValueError('Aligned explicit source rig state required')
    if not all(np.isfinite(x).all() for x in [root,rp,rc_local,limb,angle,foot,head]):raise ValueError('Finite source state required')
    rotations=np.concatenate([rp[None],rc_local[None],limb,foot])
    if not np.allclose(rotations@rotations.transpose(0,2,1),np.eye(3),atol=1e-7,rtol=0) or not np.allclose(np.linalg.det(rotations),1.,atol=1e-7,rtol=0):raise ValueError('Proper rotations required; no reflection or scale')
    if (angle<0).any() or (angle>=np.pi).any():raise ValueError('Hinges must remain on the explicit nonnegative branch below pi')
    hn=np.linalg.norm(head,axis=1,keepdims=True)
    if (hn<1e-8).any():raise ValueError('Nonzero cranial directions required')
    head=head/hn;lengths=np.array(rig['bone_lengths']);rc=rp@rc_local
    q=np.zeros((17,3));q[list(PELVIS)]=np.array(rig['pelvis_local'])@rp.T+root
    q[list(CHEST)]=np.array(rig['chest_local'])@rc.T+q[7]
    q[9]=q[8]+lengths[8]*(rc@head[0]);q[10]=q[9]+lengths[9]*(rc@head[1])
    for k,(base,middle,end) in enumerate(LIMBS):
        R=(rp if k<2 else rc)@limb[k]
        q[middle]=q[base]+R@np.array([0.,-lengths[middle-1],0.])
        q[end]=q[middle]+R@np.array([0.,-lengths[end-1]*np.cos(angle[k]),lengths[end-1]*np.sin(angle[k])])
    vertices=np.einsum('fij,vj->fvi',foot,np.array(rig['foot_vertices_local']))+q[[3,6],None,:]
    markers=np.einsum('fij,vj->fvi',foot,np.array(rig['foot_marker_local']))+q[[3,6],None,:]
    return dict(joints_world=q,foot_vertices_world=vertices,foot_support_markers_world=markers,
                pelvis_rotation_world=rp,chest_rotation_world=rc,
                foot_geometry_provenance=rig['foot_geometry_provenance'],
                world_geometry_verified=False,automatically_accepted=False)


def initialize_source_state(rig,observed_world,*,foot_rotations,previous_state=None):
    """Project a supplied source estimate into the rigid articulated shape.

    Anchored proper-rotation Procrustes fits body clusters. Straight limbs have
    no observable pole: use the supplied previous pole or a labeled fallback.
    Foot orientation is explicit, never inferred from contact presence here.
    """
    import numpy as np
    q=np.array(observed_world,float)
    if q.shape!=(17,3) or not np.isfinite(q).all():raise ValueError('Finite source estimate required')
    def rotation_fit(a,b):
        if np.linalg.matrix_rank(a,tol=1e-9)<2:raise ValueError('Body cluster reference must contain noncollinear points')
        u,_,vh=np.linalg.svd(a.T@b);fix=np.eye(3);fix[-1,-1]=np.linalg.det(vh.T@u.T)
        return vh.T@fix@u.T
    rp=rotation_fit(np.array(rig['pelvis_local']),q[list(PELVIS)]-q[0])
    rc=rotation_fit(np.array(rig['chest_local']),q[list(CHEST)]-q[7])
    limbs=[];angles=[];fallback=[]
    for k,(base,middle,end) in enumerate(LIMBS):
        v=q[middle]-q[base];w=q[end]-q[middle]
        if min(np.linalg.norm(v),np.linalg.norm(w))<1e-8:raise ValueError('Nonzero limb estimate required')
        y=-v/np.linalg.norm(v);w=w/np.linalg.norm(w);z=w-y*np.dot(y,w)
        angle=float(np.arccos(np.clip(np.dot(-y,w),-1,1)))
        if np.linalg.norm(z)<1e-7:
            fallback.append(k)
            if previous_state is not None:
                pr=np.array(previous_state['pelvis_rotation'])
                pc=pr@np.array(previous_state['chest_rotation'])
                hint=(pr if k<2 else pc)@np.array(previous_state['limb_rotations'][k])[:,2]
            else:hint=np.array([0.,0.,1.])
            z=hint-y*np.dot(hint,y)
            if np.linalg.norm(z)<1e-7:
                hint=np.eye(3)[np.argmin(abs(y))];z=hint-y*np.dot(hint,y)
        z=z/np.linalg.norm(z);x=np.cross(y,z);x/=np.linalg.norm(x);z=np.cross(x,y)
        world=np.column_stack([x,y,z]);limbs.append((rp if k<2 else rc).T@world);angles.append(min(angle,np.pi-1e-7))
    state=dict(root_world=q[0].copy(),pelvis_rotation=rp,chest_rotation=rp.T@rc,
        limb_rotations=np.array(limbs),hinge_angles=np.array(angles),foot_rotations=np.array(foot_rotations,float,copy=True),
        cranial_directions=np.array([q[9]-q[8],q[10]-q[9]])@rc)
    forward_source_rig(rig,**state)  # Fail closed on invalid input rotations/state.
    return dict(state=state,pole_fallback_limbs=fallback,observations_are_3d_truth=False,
                foot_orientation_inferred=False,world_geometry_verified=False)
