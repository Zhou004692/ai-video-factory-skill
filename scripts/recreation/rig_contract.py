"""Stdlib-only source/target separation checks, not a motion reconstruction."""
import math


def _finite_number(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)


def validate_rig(rig):
    if rig.get('schema')!='recreation-rig-contract-v1':raise ValueError('Explicit rig contract schema required')
    domain=rig.get('domain')
    expected={'source_human':('model_relative','source_model_median'),
              'target_animal':('target_relative','target_asset_authored')}
    if domain not in expected or (rig.get('unit'),rig.get('shape_provenance'))!=expected[domain]:
        raise ValueError('Source human shape/units must remain separate from target asset proportions')
    if not all(isinstance(rig.get(k),str) and rig[k] for k in ['rig_id','role_id']):raise ValueError('Stable identity and rig IDs required')
    parents=rig.get('parents',[]);lengths=rig.get('bone_lengths',[])
    if len(parents)<2 or parents[0]!=-1 or any(type(p)!=int or p<0 or p>=j for j,p in enumerate(parents[1:],1)):
        raise ValueError('Rooted topologically ordered parents required')
    if len(lengths)!=len(parents)-1 or any(not _finite_number(x) or x<=0 for x in lengths):raise ValueError('Fixed positive bone lengths required')
    scale=rig.get('body_scale')
    if not _finite_number(scale) or scale<=0:raise ValueError('One fixed body scale required, never an implicit per-frame scale')
    pose=rig.get('reference_pose',{});xyz=pose.get('xyz',[])
    if type(pose.get('source_frame_index'))!=int or pose['source_frame_index']<0:raise ValueError('Explicit reference source frame required')
    if len(xyz)!=len(parents) or any(len(p)!=3 or not all(_finite_number(v) for v in p) for p in xyz):raise ValueError('Finite reference xyz aligned to topology required')
    for j,p in enumerate(parents[1:],1):
        if not math.isclose(math.dist(xyz[j],xyz[p]),lengths[j-1],rel_tol=1e-7,abs_tol=1e-9):raise ValueError('Reference pose disagrees with fixed bone shape')
    root_status=rig.get('world_root_status')
    if root_status not in ['unresolved','estimated','validated']:raise ValueError('Explicit world-root status required')
    trajectory=rig.get('world_root_trajectory')
    if root_status=='unresolved' and trajectory is not None:raise ValueError('Unresolved source root cannot inherit a target/world-fit trajectory')
    if root_status!='unresolved':
        if not isinstance(trajectory,list) or not trajectory or any(len(p)!=3 or not all(_finite_number(v) for v in p) for p in trajectory):raise ValueError('Declared world root requires a finite explicit trajectory')
    return {'contract_valid':True,'domain':domain,'joints':len(parents),
            'world_motion_ready':root_status=='validated','naturalness_accepted':False}


def validate_retarget_pair(source,target):
    a,b=validate_rig(source),validate_rig(target)
    if a['domain']!='source_human' or b['domain']!='target_animal':raise ValueError('Map source human to a separate target rig')
    if source['rig_id']==target['rig_id'] or source['role_id']!=target['role_id']:raise ValueError('Distinct rigs and matching stable role identity required')
    if source['parents']!=target['parents']:raise ValueError('This contract requires an explicit shared joint mapping')
    return {'contract_valid':True,'role_id':source['role_id'],'world_motion_ready':a['world_motion_ready'],
            'retarget_ready':False,'reason':'Separate contracts do not certify scene, contact, bind axes or world motion',
            'naturalness_accepted':False}
