"""WholeBody observations and explicit derived lifting landmarks (stdlib only)."""
from __future__ import annotations
import hashlib
import math
from pathlib import Path
import re

SCHEMA='coco_wholebody133'


def source_body_measurements(projected_joint_xy):
    """Match the retained 2D lifting proxies without changing the 3D rig.

    Input is the image projection of the explicit source H36M-style rig.
    J0/J7/J8 observations were constructed IN 2D from hips/shoulders; a
    projected virtual 3D joint is not that measurement. Reconstruct these
    auxiliary proxies after projection and do not count them as independent
    marker constraints. Head/nose/eyes need a separate matched binding.
    """
    try:
        q=[[float(x) for x in point] for point in projected_joint_xy]
    except (TypeError,ValueError) as exc:raise ValueError('Finite source17 image projections required') from exc
    if len(q)!=17 or any(len(v)!=2 or not all(math.isfinite(x) for x in v) for v in q):raise ValueError('Finite source17 image projections required')
    measured=[v.copy() for v in q]
    measured[0]=[(q[1][k]+q[4][k])/2 for k in range(2)]
    measured[8]=[(q[11][k]+q[14][k])/2 for k in range(2)]
    measured[7]=[(measured[0][k]+measured[8][k])/2 for k in range(2)]
    measured[9]=None;measured[10]=None
    return dict(schema='source-rig-to-coco-derived-body-measurements-v1',xy_h36m17=measured,virtual_joint_image_xy=q,
                independent_fit_indices=[1,2,3,4,5,6,11,12,13,14,15,16],derived_auxiliary_indices=[0,7,8],unbound_indices=[9,10],
                rig_geometry_changed=False,raw_observations_changed=False,measurement_truth_verified=False)

def _finite(values):
    return all(isinstance(v,(int,float)) and math.isfinite(v) for v in values)

def wholebody_record(xy, scores):
    if len(xy)!=133 or len(scores)!=133:
        raise ValueError('ViTPose full migration requires 133 WholeBody landmarks')
    if any(len(p)!=2 or not _finite(p) for p in xy) or not _finite(scores):
        raise ValueError('Observations must be finite pixel coordinates and scores')
    xy=[[float(v) for v in p] for p in xy];scores=[float(v) for v in scores]
    # Follow the official MMPose COCO->H36M conversion on body17. Passing raw
    # COCO133 as if it were H36M is invalid despite the old dataset-name shortcut.
    source={1:12,2:14,3:16,4:11,5:13,6:15,9:0,11:5,12:7,13:9,14:6,15:8,16:10}
    h=[[0.,0.,0.] for _ in range(17)]
    for dst,src in source.items():h[dst]=xy[src]+[max(0.,min(1.,scores[src]))]
    def midpoint(a,b):return [(a[k]+b[k])/2 for k in range(2)]+[min(a[2],b[2])]
    h[0]=midpoint(h[1],h[4]);h[8]=midpoint(h[11],h[14]);h[7]=midpoint(h[0],h[8])
    h[10]=midpoint(xy[1]+[max(0.,min(1.,scores[1]))],xy[2]+[max(0.,min(1.,scores[2]))])
    return {'schema':SCHEMA,'xy133':xy,'scores133':scores,'body_xy17':xy[:17],
            'feet_xy6':xy[17:23],'face_xy68':xy[23:91],
            'hand_left_xy21':xy[91:112],'hand_right_xy21':xy[112:133],
            'h36m17':h,'h36m_derived_indices':[0,7,8,10],
            'h36m_head10_semantics':'eye-midpoint proxy per MMPose COCO mapping; not an observed crown',
            'raw_observations_retained':True,'contact_or_visibility_truth':False}

def file_sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk:=f.read(1024*1024):h.update(chunk)
    return h.hexdigest()

def validate_model_manifest(manifest):
    value=manifest.get('onnx','')
    if not isinstance(value,str) or value.startswith(('http:','https:')) or not Path(value).is_file():
        raise ValueError('An explicit existing local ONNX model is required; no automatic download')
    if manifest.get('family')!='vitpose' or manifest.get('schema')!=SCHEMA:
        raise ValueError('ViTPose must use the 133-landmark WholeBody task head')
    if manifest.get('decoder') not in ['mmpose-msra-default','mmpose-udp']:
        raise ValueError('Declare the decoder matching the original training configuration')
    size=manifest.get('input_size_wh')
    if not isinstance(size,list) or len(size)!=2 or any(type(v)!=int or v<=0 for v in size):
        raise ValueError('Declare positive integer input_size_wh')
    digest=manifest.get('sha256','')
    if not re.fullmatch('[0-9a-f]{64}',digest) or file_sha256(value)!=digest:
        raise ValueError('Model SHA256 mismatch')
    return {'keypoints':133,'model_path':str(Path(value).resolve()),'sha256':digest,
            'input_size_wh':size,'decoder':manifest['decoder']}
