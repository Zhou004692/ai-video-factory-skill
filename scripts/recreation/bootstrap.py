"""Independent, local-only recreation runtime. No production pipeline imports."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

SKILL_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
STAGES = {
    'inspect': (),
    'pose': ('numpy>=1.26,<3', 'opencv-python-headless>=4.10,<6', 'onnxruntime>=1.20,<2'),
    'fit': ('numpy>=1.26,<3', 'scipy>=1.11,<2'),
    'render': (),
}
IMPORTS = {'pose': ('numpy', 'cv2', 'onnxruntime'), 'fit': ('numpy', 'scipy')}

def default_home() -> Path:
    override = os.environ.get('AI_VIDEO_RECREATION_HOME')
    if override:
        return Path(override).expanduser()
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData/Local')
    elif sys.platform == 'darwin':
        base = Path.home()/'Library/Application Support'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')
    return base/'ai-video-factory-recreation'

def _run(args: list[str], timeout: int = 60) -> dict:
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'returncode': -1, 'stdout': '', 'stderr': type(exc).__name__}

def _python(home: Path, stage: str) -> Path:
    return home/'venvs'/stage/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

def _packages(python: Path, stage: str) -> dict:
    names = IMPORTS.get(stage, ())
    if not names:
        return {'ok': True, 'missing': []}
    if not python.is_file():
        return {'ok': False, 'missing': list(names)}
    # Import libraries only: no model construction, downloads or pipeline imports.
    code = ('import importlib,importlib.metadata as meta,json,re\nmissing=[]\n'
            f'for name in {names!r}:\n'
            ' try: importlib.import_module(name)\n'
            ' except Exception: missing.append(name)\n'
            f'requirements={STAGES[stage]!r}\n'
            'versions={};incompatible=[]\n'
            'def version(v):\n'
            ' return tuple(map(int,v.split("."))) if re.fullmatch(r"\\d+(?:\\.\\d+)*",v) else None\n'
            'for req in requirements:\n'
            ' name,bounds=re.match(r"^([^<>=]+)(.*)$",req).groups()\n'
            ' try: value=meta.version(name)\n'
            ' except meta.PackageNotFoundError: incompatible.append(name+": absent");continue\n'
            ' versions[name]=value;current=version(value)\n'
            ' for operator,wanted in re.findall(r"(>=|<|==)([0-9.]+)",bounds):\n'
            '  target=version(wanted)\n'
            '  if current is None or not {">=":lambda:current>=target,"<":lambda:current<target,"==":lambda:value==wanted}[operator](): incompatible.append(req)\n'
            'opencv=[]\n'
            'for name in ("opencv-python","opencv-contrib-python","opencv-contrib-python-headless","opencv-python-headless"):\n'
            ' try: meta.version(name);opencv.append(name)\n'
            ' except meta.PackageNotFoundError: pass\n'
            f'if {stage!r}=="pose" and opencv!=["opencv-python-headless"]: incompatible.append("use exactly one opencv-python-headless distribution")\n'
            'print(json.dumps({"ok":not missing and not incompatible,"missing":missing,"incompatible":incompatible,"versions":versions,"opencv_distributions":opencv}))')
    result = _run([str(python), '-I', '-c', code])
    try:
        report = json.loads(result['stdout'].strip().splitlines()[-1])
        return report if result['returncode'] == 0 else {'ok': False, 'missing': list(names)}
    except (ValueError, IndexError):
        return {'ok': False, 'missing': list(names)}

def _tool(name: str) -> dict:
    path = shutil.which(name)
    if not path:
        return {'ok': False, 'detail': 'not found'}
    r = _run([path, '--version' if name == 'node' else '-version'])
    ok = r['returncode'] == 0
    if name == 'node':
        major = re.match(r'^v?(\d+)\.', r['stdout'].strip())
        ok = ok and bool(major and int(major.group(1)) >= 22)
    return {'ok': ok, 'path': path}

def bootstrap(*, stage: str = 'inspect', runtime_root: Path | None = None,
              install: bool = False, check_only: bool = False) -> dict:
    if stage not in STAGES:
        raise ValueError('Unknown recreation stage')
    if install and check_only:
        raise ValueError('--install and --check-only are mutually exclusive')
    home = (runtime_root or default_home()).expanduser().resolve()
    if home == SKILL_ROOT or home.is_relative_to(SKILL_ROOT):
        raise ValueError('Recreation runtime must stay outside the replaceable Skill directory')
    deps = list(STAGES[stage])
    python = _python(home, stage) if deps else Path(sys.executable)
    installed = False
    if install and deps:
        if not python.is_file():
            r = _run([sys.executable, '-I', '-m', 'venv', str(python.parents[1])], timeout=120)
            if r['returncode'] != 0:
                return _report(stage, home, python, deps, {}, {'ok': False, 'missing': list(IMPORTS[stage])}, False, 'dependency_install_failed')
        if not _packages(python, stage)['ok']:
            commands = [[str(python), '-I', '-m', 'pip', 'install', '--disable-pip-version-check', *deps]]
            for cmd in commands:
                if _run(cmd, timeout=900)['returncode'] != 0:
                    return _report(stage, home, python, deps, {}, _packages(python, stage), False, 'dependency_install_failed')
            installed = True
    packages = _packages(python, stage)
    tools = {name: _tool(name) for name in ('ffmpeg', 'ffprobe')}
    if stage == 'render':
        tools['node'] = _tool('node')
    status = 'dependencies_required' if not packages['ok'] else ('runtime_required' if not all(x['ok'] for x in tools.values()) else 'ready')
    report = _report(stage, home, python, deps, tools, packages, installed, status)
    if not check_only:
        home.mkdir(parents=True, exist_ok=True)
        # Only stage readiness is recorded. Existing project and model files are
        # untouched; failed reruns do not erase prior stage observations.
        state_path = home/'runtime.json'
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {'schema_version': 1, 'feature': 'recreation', 'stages': {}}
        if state.get('feature') != 'recreation' or state.get('schema_version') != 1:
            raise ValueError('Unsupported recreation runtime state; preserve and migrate explicitly')
        state['stages'][stage] = report
        fd, temp = tempfile.mkstemp(prefix='.runtime-', suffix='.json', dir=home)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(state, out, ensure_ascii=False, indent=2)
            os.replace(temp, state_path)
        finally:
            if Path(temp).exists():
                Path(temp).unlink()
    return report

def _report(stage, home, python, deps, tools, packages, installed, status):
    return {'feature': 'recreation', 'stage': stage, 'status': status, 'ready': status == 'ready',
            'readiness_scope': 'selected stage dependencies only; not model/rig readiness or finished recreation',
            'runtime_root': str(home), 'runtime_python': str(python), 'python_dependencies': deps,
            'packages': packages, 'tools': tools, 'installed_dependencies': installed,
            'model_downloads': 0, 'paid_provider_calls': 0, 'source_uploads': 0,
            'production_pipeline_initialized': False,
            'pose_model_family': 'vitpose', 'pose_schema': 'coco_wholebody133',
            'pose_decoder': 'mmpose-msra-default'}

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage', choices=tuple(STAGES), default='inspect')
    p.add_argument('--runtime-root', type=Path)
    options = p.add_mutually_exclusive_group()
    options.add_argument('--install', action='store_true', help='Install only selected stage Python dependencies; no model weights')
    options.add_argument('--check-only', action='store_true', help='Read-only checks; never create state or venv')
    p.add_argument('--json', action='store_true')
    args = p.parse_args()
    try:
        r = bootstrap(stage=args.stage, runtime_root=args.runtime_root, install=args.install, check_only=args.check_only)
    except (ValueError, OSError) as exc:
        r = {'feature': 'recreation', 'status': 'runtime_required', 'ready': False, 'error': str(exc), 'paid_provider_calls': 0, 'source_uploads': 0}
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else f"recreation: {r['status']}")
    return 0 if r['ready'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
