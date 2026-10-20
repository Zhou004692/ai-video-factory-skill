"""Explicit, hash-bound CPU pose asset preparation; no implicit downloads."""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.request import urlopen

SKILL_ROOT=Path(__file__).resolve().parents[2]
MODEL_SPEC={
    'id':'vitpose-wholebody-b','family':'vitpose','schema':'coco_wholebody133',
    'input_size_wh':[192,256],'decoder':'mmpose-msra-default',
    'bytes':360242862,'sha256':'017c08e161bfa2c72a96297b3b6de975ecd83c65c4fd56fb5b0bfc1298709069',
    'url':'https://huggingface.co/JunkyByte/easy_ViTPose/resolve/e83805274e89428969355ec4afffcbc413e79188/onnx/wholebody/vitpose-b-wholebody.onnx',
    'source_revision':'e83805274e89428969355ec4afffcbc413e79188',
    'source_repository':'https://github.com/ViTAE-Transformer/ViTPose',
    'conversion_repository':'https://huggingface.co/JunkyByte/easy_ViTPose',
    'license_notice':'Upstream code: Apache-2.0. ONNX is a third-party conversion; consult upstream and conversion model terms separately. Weight is not bundled.',
}

def _digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while block:=f.read(1024*1024):h.update(block)
    return h.hexdigest()

def _external(path):
    value=Path(path).expanduser().resolve()
    if value==SKILL_ROOT or value.is_relative_to(SKILL_ROOT):
        raise ValueError('Runtime and model weights must remain outside the replaceable Skill')
    return value

def _runtime_path(runtime_root,*parts):
    home=_external(runtime_root)
    path=_external(home.joinpath(*parts))
    if not path.is_relative_to(home):raise ValueError('Model state must remain inside the selected runtime; symlink escape rejected')
    return path

def manifest_path(runtime_root):
    return _runtime_path(runtime_root,'models','vitpose-wholebody-b','model-manifest.json')

def _acquire_lock(directory):
    path=directory/'.prepare.lock'
    if path.is_symlink():raise ValueError('Model preparation lock must not be a symlink')
    stream=path.open('a+b')
    if path.stat().st_size==0:stream.write(b'0');stream.flush()
    deadline=time.monotonic()+10
    while True:
        try:
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            return stream
        except OSError:
            if time.monotonic()>=deadline:
                stream.close();raise ValueError('Model preparation is active in another process')
            time.sleep(.025)

def _release_lock(stream):
    try:
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
    finally:stream.close()

def model_status(runtime_root, *, manifest=None):
    path=_external(manifest) if manifest else manifest_path(runtime_root)
    report={'ready':False,'manifest_path':str(path),'model_downloads':0,'source_uploads':0,'paid_provider_calls':0}
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        weight=_external(data['onnx'])
        if (data.get('family')!=MODEL_SPEC['family'] or data.get('schema')!=MODEL_SPEC['schema']
            or data.get('decoder')!=MODEL_SPEC['decoder'] or data.get('input_size_wh')!=MODEL_SPEC['input_size_wh']):
            raise ValueError('Manifest does not describe the supported WholeBody task')
        if data.get('sha256')!=MODEL_SPEC['sha256'] or weight.stat().st_size!=MODEL_SPEC['bytes'] or _digest(weight)!=MODEL_SPEC['sha256']:
            raise ValueError('Pose weight size or SHA256 does not match the pinned asset')
        report.update(ready=True,model_path=str(weight),sha256=MODEL_SPEC['sha256'],bytes=MODEL_SPEC['bytes'])
    except (OSError,ValueError,KeyError,TypeError) as exc:
        report.update(reason=type(exc).__name__ if isinstance(exc,OSError) else str(exc))
    return report

def prepare_model(runtime_root, *, source_path=None, download_approved=False, open_url=None):
    home=_external(runtime_root)
    existing=model_status(home)
    if source_path is None and existing['ready']:
        return dict(existing,model_downloads=0,registered_cached_model=True)
    if source_path is None and download_approved is not True:
        raise ValueError('Explicit weight download approval or a selected existing local model is required')
    selected=_external(source_path) if source_path is not None else None
    if selected is not None and (not selected.is_file() or selected.stat().st_size!=MODEL_SPEC['bytes'] or _digest(selected)!=MODEL_SPEC['sha256']):
        raise ValueError('Selected local model does not match pinned WholeBody weight')
    directory=manifest_path(home).parent;directory.mkdir(parents=True,exist_ok=True)
    lock=_acquire_lock(directory)
    part=None;downloads=0
    try:
        if selected is None:
            selected=_runtime_path(home,'models','vitpose-wholebody-b','vitpose-b-wholebody.onnx')
            if selected.exists():
                if selected.stat().st_size!=MODEL_SPEC['bytes'] or _digest(selected)!=MODEL_SPEC['sha256']:
                    raise ValueError('Existing weight differs; preserve it and choose an explicit verified model')
            else:
                fd,name=tempfile.mkstemp(prefix='.weight-',suffix='.part',dir=directory);part=Path(name)
                count=0;digest=hashlib.sha256()
                with os.fdopen(fd,'wb') as dest,closing((open_url or urlopen)(MODEL_SPEC['url'],timeout=60)) as response:
                    while block:=response.read(1024*1024):
                        count+=len(block)
                        if count>MODEL_SPEC['bytes']:raise ValueError('Model response exceeds declared byte size')
                        digest.update(block);dest.write(block)
                    dest.flush();os.fsync(dest.fileno())
                if count!=MODEL_SPEC['bytes'] or digest.hexdigest()!=MODEL_SPEC['sha256']:
                    raise ValueError('Downloaded model size or SHA256 mismatch')
                os.replace(part,selected);part=None;downloads=1
        data={k:v for k,v in MODEL_SPEC.items() if k not in ['id','url','bytes']}
        data.update(onnx=str(selected),source_url=MODEL_SPEC['url'],flip_test=True,shift_heatmap=True)
        fd,name=tempfile.mkstemp(prefix='.manifest-',suffix='.part',dir=directory)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
            os.replace(name,manifest_path(home))
        finally:
            if Path(name).exists():Path(name).unlink()
        return dict(model_status(home),model_downloads=downloads,registered_cached_model=downloads==0)
    finally:
        if part is not None and part.exists():part.unlink()
        _release_lock(lock)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runtime-root',type=Path,required=True)
    p.add_argument('--source',type=Path);p.add_argument('--download-approved',action='store_true');p.add_argument('--describe',action='store_true')
    a=p.parse_args()
    if a.describe:r=dict(MODEL_SPEC,automatic_download=False,model_downloads=0)
    else:r=prepare_model(a.runtime_root,source_path=a.source,download_approved=a.download_approved)
    print(json.dumps(r,ensure_ascii=False,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
