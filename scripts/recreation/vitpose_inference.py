"""Local WholeBody133 ViTPose inference matching the official MSRA demo.

Only explicit local, hash-bound models and source-bound identity tracks are used.
No provider, production pipeline, downloads or implicit RTMPose fallback.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import numpy as np
import cv2
from pose_schema import file_sha256, validate_model_manifest, wholebody_record
from vendor_mmpose_msra import get_affine_transform, keypoints_from_heatmaps, flip_back

HERE=Path(__file__).resolve().parent
PAIRS=json.loads((HERE/'wholebody133.json').read_text(encoding='utf-8'))['flip_pairs']

def preprocess(image_bgr, bbox_xyxy, input_size_wh):
    """Official inclusive xyxy -> xywh -> center/scale -> RGB affine."""
    x,y,x2,y2=map(float,bbox_xyxy)
    w,h=x2-x+1,y2-y+1
    if w<=0 or h<=0 or not np.isfinite([x,y,w,h]).all():
        raise ValueError('A finite positive inclusive bbox is required')
    center=np.array([x+w*.5,y+h*.5],dtype=np.float32)
    aspect=input_size_wh[0]/input_size_wh[1]
    if w>h*aspect:h=w/aspect
    else:w=h*aspect
    scale=np.array([w/200,h/200],np.float32)*1.25
    trans=get_affine_transform(center,scale,0,input_size_wh)
    rgb=cv2.cvtColor(image_bgr,cv2.COLOR_BGR2RGB)
    crop=cv2.warpAffine(rgb,trans,tuple(input_size_wh),flags=cv2.INTER_LINEAR)
    a=(crop.astype(np.float32)/255-np.array([.485,.456,.406],np.float32))/np.array([.229,.224,.225],np.float32)
    return np.ascontiguousarray(a.transpose(2,0,1)[None]),center,scale

def flip_heatmaps(heatmaps, *, shift=True):
    out=flip_back(heatmaps,PAIRS,target_type='GaussianHeatmap')
    if shift:out[:,:,:,1:]=out[:,:,:,:-1].copy()
    return out

def decode_heatmaps(heatmaps, centers, scales):
    if heatmaps.ndim!=4 or heatmaps.shape[1]!=133:
        raise ValueError('Expected WholeBody133 task heatmaps')
    return keypoints_from_heatmaps(heatmaps,centers,scales,unbiased=False,
        post_process='default',kernel=11,use_udp=False)

class ViTPose:
    def __init__(self, manifest, *, threads=4):
        import onnxruntime as ort
        self.model=validate_model_manifest(manifest)
        if self.model['decoder']!='mmpose-msra-default':
            raise ValueError('This demo adapter only supports the verified MSRA configuration')
        opts=ort.SessionOptions();opts.intra_op_num_threads=threads;opts.inter_op_num_threads=1
        self.session=ort.InferenceSession(self.model['model_path'],sess_options=opts,providers=['CPUExecutionProvider'])
        self.input_name=self.session.get_inputs()[0].name
        if self.session.get_inputs()[0].shape[1:]!=[3,self.model['input_size_wh'][1],self.model['input_size_wh'][0]]:
            raise ValueError('Model input shape differs from its manifest')

    def heatmaps(self, x, *, flip_test=True):
        h=self.session.run(None,{self.input_name:x})[0]
        if h.ndim!=4 or h.shape[1]!=133 or not np.isfinite(h).all():
            raise ValueError('Invalid WholeBody133 heatmaps')
        if flip_test:
            flipped=self.session.run(None,{self.input_name:np.ascontiguousarray(x[:,:,:,::-1])})[0]
            h=(h+flip_heatmaps(flipped))/2
        return h

    def predict(self, image, boxes):
        pre=[preprocess(image,b,self.model['input_size_wh']) for b in boxes]
        if not pre:return []
        x=np.concatenate([a[0] for a in pre]);c=np.stack([a[1] for a in pre]);s=np.stack([a[2] for a in pre])
        xy,score=decode_heatmaps(self.heatmaps(x),c,s)
        return [wholebody_record(p.tolist(),v[:,0].tolist()) for p,v in zip(xy,score)]

def run(source, tracks_path, manifest_path, output):
    if output.exists():raise ValueError('Refusing to overwrite observations; choose a new output')
    tracks=json.loads(tracks_path.read_text(encoding='utf-8'))
    sha=file_sha256(source)
    if tracks.get('source_sha256')!=sha:raise ValueError('Identity tracks do not match source SHA256')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'));model=ViTPose(manifest)
    rows=tracks['rows'];indices=[r['source_frame_index'] for r in rows]
    if indices!=list(range(len(rows))):raise ValueError('Complete contiguous tracks starting at frame zero required')
    x,y,w,h=tracks['crop_xywh'];cap=cv2.VideoCapture(str(source));fps=cap.get(cv2.CAP_PROP_FPS)
    if abs(fps-tracks['fps'])>1e-4:raise ValueError('Track FPS differs from video')
    if int(cap.get(cv2.CAP_PROP_FRAME_COUNT))!=len(rows):raise ValueError('Track/video frame count mismatch')
    result=[];start=time.monotonic()
    try:
        for row in rows:
            ok,frame=cap.read()
            if not ok:raise ValueError('Video ended before all identity tracks')
            if frame[y:y+h,x:x+w].shape[:2]!=(h,w):raise ValueError('Crop extends outside video')
            roles=row['roles'];ids=[r['id'] for r in roles]
            if len(set(ids))!=len(ids):raise ValueError('Duplicate role identities')
            active=[r for r in roles if r.get('bbox_xyxy') is not None]
            records=model.predict(frame[y:y+h,x:x+w],[r['bbox_xyxy'] for r in active])
            byid={r['id']:p for r,p in zip(active,records)}
            out=[]
            for role in roles:
                record={'id':role['id'],'bbox_xyxy':role.get('bbox_xyxy'),
                        'identity_provenance':role.get('identity_provenance'),
                        'uncertainty':role.get('uncertainty',[])}
                record.update(byid.get(role['id'],{'missing_observation':True}))
                out.append(record)
            n=row['source_frame_index']
            result.append({'source_frame_index':n,'timestamp_seconds':float(cap.get(cv2.CAP_PROP_POS_MSEC))/1000,'roles':out})
            if n%30==0:print(json.dumps({'frame':n,'total':len(rows),'elapsed_s':round(time.monotonic()-start,1)}),flush=True)
    finally:cap.release()
    report={'schema_version':1,'pose_family':'vitpose','keypoint_schema':'coco_wholebody133',
            'source_sha256':sha,'source_path':str(source.resolve()),'fps':fps,'crop_xywh':[x,y,w,h],
            'model':model.model,'model_manifest':manifest,'tracks_sha256':file_sha256(tracks_path),
            'preprocess':'official RGB TopDownAffine, bbox inclusive +1, padding 1.25',
            'decode':'MSRA/default, flip_test=True, shift_heatmap=True','rows':result,
            'elapsed_s':time.monotonic()-start,'source_uploads':0,'paid_provider_calls':0,
            'contact_or_motion_fidelity_accepted':False}
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False)
    return report

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['source','tracks','model-manifest','output']:p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();r=run(a.source,a.tracks,a.model_manifest,a.output)
    print(json.dumps({'output':str(a.output),'frames':len(r['rows']),'elapsed_s':r['elapsed_s']}))

if __name__=='__main__':main()
