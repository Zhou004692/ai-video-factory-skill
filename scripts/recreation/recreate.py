"""Agent-operated independent recreation entry. Local work never submits video."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import html
import json
from pathlib import Path
import subprocess
import sys
import uuid

HERE=Path(__file__).resolve().parent
# Explicit installed code directory, including when launched with python -I.
sys.path.insert(0,str(HERE))
import bootstrap
from inspect_media import inspect_media

class StageStopped(Exception):
    def __init__(self,report):self.report=report

def _emit(report):
    print(json.dumps(report,ensure_ascii=False,indent=2))

def _stage_python(runtime,stage):
    report=bootstrap.bootstrap(stage=stage,runtime_root=runtime,check_only=True)
    if not report['ready']:
        raise StageStopped(dict(status=report['status'],required_stage=stage,initialization=report,
                                paid_provider_calls=0,source_uploads=0))
    return report['runtime_python']

def _child(runtime,stage,script,args):
    python=_stage_python(runtime,stage)
    command="import os,runpy,sys;path=sys.argv.pop(1);sys.path.insert(0,os.path.dirname(path));runpy.run_path(path,run_name='__main__')"
    result=subprocess.run([python,'-I','-c',command,str(HERE/script),*map(str,args)],
                          capture_output=True,text=True,encoding='utf-8',errors='replace')
    if result.returncode:
        raise StageStopped(dict(status='stage_failed',stage=stage,script=script,
                                detail=(result.stderr or result.stdout)[-1800:],paid_provider_calls=0,source_uploads=0))
    return result.stdout

def _roles(path):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    return data['roles'] if isinstance(data,dict) else data

def _next_path(project,kind,suffix):
    root=Path(project).resolve()
    directory=(root/kind).resolve()
    if directory==root or not directory.is_relative_to(root):raise ValueError('Stage output must remain inside project')
    directory.mkdir(parents=True,exist_ok=True)
    if directory.resolve()!=directory:raise ValueError('Stage output directory changed during creation')
    return directory/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]+suffix)

def inspect_source(source,output):
    facts=inspect_media(Path(source));directory=Path(output).expanduser().resolve()
    skill=HERE.parents[1]
    if directory==skill or directory.is_relative_to(skill):raise ValueError('Inspection output must stay outside Skill')
    directory.mkdir(parents=True,exist_ok=True)
    frame=directory/'first-frame.jpg';page=directory/'selection.html'
    if frame.exists() or page.exists():raise ValueError('Inspection already exists; choose a new output directory')
    subprocess.run(['ffmpeg','-v','error','-threads','2','-i',facts['source'],'-frames:v','1','-q:v','2',str(frame)],
                   capture_output=True,check=True)
    content='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>复刻人物范围</title><style>body{font:16px/1.7 sans-serif;max-width:1100px;margin:24px auto;background:#f4f6f9;color:#233047}img,video{max-width:100%;max-height:75vh}section{background:white;padding:20px;margin:16px 0}code{word-break:break-all}</style><h1>先确认替换对象</h1><p>请说明替换哪个人物、各自对应的动物，以及其他人物保留还是移除。Agent将记录所选身份和锚框；候选框不自动视为身份已确认。</p><section><h2>原片首帧</h2><img src="first-frame.jpg" alt="本地原片首帧"></section><section><h2>原片（仅本地）</h2><video controls src="__SOURCE__"></video></section><p>人物后续才入画时，使用其实际出现帧作为锚；遮挡与交叉需复核，不靠自动编号替代人物选择。</p></html>'''
    page.write_text(content.replace('__SOURCE__',html.escape(Path(facts['source']).as_uri(),quote=True)),encoding='utf-8')
    (directory/'source-facts.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
    return dict(status='selection_required',selection_page=str(page),source=facts,paid_provider_calls=0,source_uploads=0)

def track(project,runtime):
    import project_store as store
    before=store.load_project(project)
    output=_next_path(project,'tracks','.json')
    _child(runtime,'pose','identity_tracks.py',['--project',project,'--output',output])
    if store.load_project(project)['binding_sha256']!=before['binding_sha256']:raise ValueError('Roles changed during tracking')
    return store.get_artifact(project,'tracks')

def observe(project,runtime,manifest=None):
    import project_store as store
    from model_assets import model_status
    _stage_python(runtime,'pose')
    resources=model_status(runtime,manifest=manifest)
    if not resources['ready']:
        raise StageStopped(dict(status='model_required',resources=resources,paid_provider_calls=0,source_uploads=0))
    before=store.load_project(project);tracks=store.get_artifact(project,'tracks')
    output=_next_path(project,'observations','.json')
    _child(runtime,'pose','vitpose_inference.py',['--source',before['source']['path'],'--tracks',tracks['path'],
            '--model-manifest',resources['manifest_path'],'--output',output])
    if store.load_project(project)['binding_sha256']!=before['binding_sha256']:raise ValueError('Roles changed during inference')
    return store.record_artifact(project,'observations',output)

def preview(project,runtime,settings=None):
    import project_store as store
    _stage_python(runtime,'render')
    from render_preview import render_preview,resource_report
    resources=resource_report(settings)
    if not resources['ready']:
        raise StageStopped(dict(status='render_resources_required',resources=resources,paid_provider_calls=0,source_uploads=0))
    before=store.load_project(project);observations=store.get_artifact(project,'observations')
    directory=_next_path(project,'candidates','')
    report=render_preview(project,observations['path'],directory,settings=settings)
    if store.load_project(project)['binding_sha256']!=before['binding_sha256']:raise ValueError('Roles changed during rendering')
    published=dict(report['artifacts'])
    published['render_report']=directory/'report.json'
    artifacts=store.record_artifacts(project,published)
    return dict(status='CG_review_required',artifacts=artifacts,render=report,paid_provider_calls=0,source_uploads=0)

def status(project):
    import project_store as store
    state=store.load_project(project);artifacts={}
    for name,item in state['artifacts'].items():
        if item.get('status')=='stale':artifacts[name]=dict(status='stale');continue
        artifacts[name]=store.get_artifact(project,name)
    return dict(project_id=state['project_id'],stage=state['stage'],source_frames=state['source']['frame_count'],
                roles=[dict(id=r['id'],animal=r['animal']) for r in state['roles']],artifacts=artifacts,
                paid_provider_calls=0,source_uploads=0,
                next_action='Review current CG before optional model/budget planning; local commands never upload or submit')

def local(project,runtime,manifest=None,settings=None):
    import project_store as store
    state=store.load_project(project)
    def current(name):
        item=state['artifacts'].get(name)
        return item is not None and item.get('status')!='stale'
    if all(current(k) for k in ['cg_silent','comparison','review']):
        report=status(project);report.update(status='existing_CG_review',resumed=True);return report
    if not current('tracks'):
        track(project,runtime);state=store.load_project(project)
    if not current('observations'):
        observe(project,runtime,manifest);state=store.load_project(project)
    return preview(project,runtime,settings)

def generation_command(runtime,arguments):
    # Only a new TOS upload needs its optional SDK environment. Planning,
    # HTTPS references, reconciliation and downloads remain standard-library.
    uses_tos=bool(arguments and arguments[0] in ('upload','verify-upload') and '--reference-config-file' not in arguments
                  and not any(x.startswith('--reference-config-file=') for x in arguments))
    python=_stage_python(runtime,'generate-tos') if uses_tos else sys.executable
    command="import os,runpy,sys;path=sys.argv.pop(1);sys.path.insert(0,os.path.dirname(path));runpy.run_path(path,run_name='__main__')"
    return subprocess.run([str(python),'-I','-c',command,str(HERE/'generation.py'),*arguments]).returncode

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime-root',type=Path,default=None)
    sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('inspect');q.add_argument('--source',type=Path,required=True);q.add_argument('--output',type=Path,required=True)
    q=sub.add_parser('init');q.add_argument('--source',type=Path,required=True);q.add_argument('--project',type=Path,required=True)
    q.add_argument('--roles',type=Path,required=True);q.add_argument('--crop',help='crop-relative origin in source: x,y,width,height')
    q=sub.add_parser('select');q.add_argument('--project',type=Path,required=True);q.add_argument('--roles',type=Path,required=True);q.add_argument('--confirmation',required=True)
    for name in ['status','track','observe','preview','local']:
        q=sub.add_parser(name);q.add_argument('--project',type=Path,required=True)
        if name in ['observe','local']:q.add_argument('--model-manifest',type=Path)
        if name in ['preview','local']:q.add_argument('--browser-path');q.add_argument('--width',type=int,default=1280)
    q=sub.add_parser('accept');q.add_argument('--project',type=Path,required=True);q.add_argument('--artifact',required=True)
    q.add_argument('--sha256',required=True);q.add_argument('--confirmation',required=True)
    q=sub.add_parser('doctor');q.add_argument('--stage',choices=['inspect','pose','fit','render'],default='inspect');q.add_argument('--model-manifest',type=Path)
    q=sub.add_parser('model');q.add_argument('--source',type=Path);q.add_argument('--download-approved',action='store_true');q.add_argument('--describe',action='store_true')
    q=sub.add_parser('generation');q.add_argument('arguments',nargs=argparse.REMAINDER)
    a=p.parse_args();runtime=(a.runtime_root or bootstrap.default_home()).expanduser().resolve()
    try:
        if a.command=='inspect':r=inspect_source(a.source,a.output)
        elif a.command=='init':
            import project_store as store
            crop=[int(x) for x in a.crop.split(',')] if a.crop else None
            state=store.init_project(a.source,a.project,_roles(a.roles),crop)
            r=dict(status='initialized',project=str(a.project.resolve()),project_id=state['project_id'],paid_provider_calls=0,source_uploads=0)
        elif a.command=='select':
            import project_store as store
            r=store.update_roles(a.project,_roles(a.roles),a.confirmation)
        elif a.command=='status':r=status(a.project)
        elif a.command=='track':r=track(a.project,runtime)
        elif a.command=='observe':r=observe(a.project,runtime,a.model_manifest)
        elif a.command in ['preview','local']:
            settings={'width':a.width}
            if a.browser_path:settings['browser_path']=a.browser_path
            r=local(a.project,runtime,a.model_manifest,settings) if a.command=='local' else preview(a.project,runtime,settings)
        elif a.command=='accept':
            import project_store as store
            r=store.accept_artifact(a.project,a.artifact,a.sha256,a.confirmation)
        elif a.command=='doctor':
            from doctor import check_runtime
            r=check_runtime(runtime,stage=a.stage,manifest=a.model_manifest)
            _emit(r);return 0 if r['execution_ready'] else 1
        elif a.command=='model':
            from model_assets import prepare_model,MODEL_SPEC
            r=dict(MODEL_SPEC,automatic_download=False) if a.describe else prepare_model(runtime,source_path=a.source,download_approved=a.download_approved)
        elif a.command=='generation':
            # The independent adapter owns model/budget/approval gates. No local
            # command falls through into this branch or copies an old approval.
            return generation_command(runtime,a.arguments)
        _emit(r);return 0
    except StageStopped as exc:_emit(exc.report);return 1
    except (ValueError,OSError,KeyError,subprocess.SubprocessError) as exc:
        _emit(dict(status='stopped',error_type=type(exc).__name__,detail=str(exc)[-1400:],paid_provider_calls=0,source_uploads=0));return 1

if __name__=='__main__':raise SystemExit(main())
