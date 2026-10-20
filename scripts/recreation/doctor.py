"""Read-only execution readiness, distinct from dependency-only bootstrap."""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import sys

HERE=Path(__file__).resolve().parent

def _module(name):
    spec=importlib.util.spec_from_file_location('recreation_doctor_'+name,HERE/(name+'.py'))
    mod=importlib.util.module_from_spec(spec)
    sys.path.insert(0,str(HERE))
    try:spec.loader.exec_module(mod)
    finally:sys.path.pop(0)
    return mod

def dependency_status(runtime_root,stage):
    return _module('bootstrap').bootstrap(stage=stage,runtime_root=Path(runtime_root),check_only=True)

def model_status(runtime_root,manifest=None):
    return _module('model_assets').model_status(runtime_root,manifest=manifest)

def render_resources(settings=None):
    try:return _module('render_preview').resource_report(settings)
    except (ImportError,OSError,ValueError,AttributeError) as exc:
        return {'ready':False,'missing':['portable render module or resources'],'error_type':type(exc).__name__}

def check_runtime(runtime_root, *, stage='inspect', manifest=None, settings=None):
    if stage not in ['inspect','pose','fit','render']:raise ValueError('Unknown local recreation stage')
    deps=dependency_status(runtime_root,stage);resources={}
    if stage=='pose':resources['model']=model_status(runtime_root,manifest)
    if stage=='render':resources['render']=render_resources(settings)
    ready=bool(deps.get('ready')) and all(r.get('ready') is True for r in resources.values())
    return {'stage':stage,'dependencies_ready':bool(deps.get('ready')),'execution_ready':ready,
            'dependencies':deps,'resources':resources,'source_uploads':0,'paid_provider_calls':0,'model_downloads':0,
            'scope':'Selected executable stage prerequisites only; project inputs, CG acceptance and output quality require separate checks'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--runtime-root',type=Path,required=True)
    p.add_argument('--stage',choices=['inspect','pose','fit','render'],default='inspect');p.add_argument('--model-manifest',type=Path)
    a=p.parse_args();r=check_runtime(a.runtime_root,stage=a.stage,manifest=a.model_manifest)
    print(json.dumps(r,ensure_ascii=False,indent=2));return 0 if r['execution_ready'] else 1

if __name__=='__main__':raise SystemExit(main())
