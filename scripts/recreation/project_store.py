"""Standard-library, source-bound portable recreation state. No factory database.

State publication uses a process lock and fsynced atomic replacement. Interrupted
unpublished temporary files are ignored; OS locks release after process death.
Source/decoded frame facts are immutable. Reviewed anchor changes are explicit.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

SCHEMA = 'animal-recreation-project-v1'
LOCK_TIMEOUT_SECONDS = 10

def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
    return digest.hexdigest()

def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()

def _now(): return datetime.now(timezone.utc).isoformat()

def _root(project_dir):
    root = Path(project_dir).expanduser().resolve()
    skill_root = Path(__file__).resolve().parents[2]
    if root == skill_root or skill_root in root.parents:
        raise ValueError('Project must live outside replaceable Skill')
    return root

def _internal(root, path, *, exists=True):
    path = Path(path)
    if not path.is_absolute(): path = root / path
    # Resolve every existing symlink/junction before checking containment.
    resolved = path.resolve(strict=exists)
    try: resolved.relative_to(root)
    except ValueError as exc: raise ValueError('Artifact must remain under project directory') from exc
    if exists and not resolved.is_file(): raise ValueError('Artifact must be a regular file')
    if resolved == root / 'project.json' or resolved.name.startswith('.project'):
        raise ValueError('Project state/lock cannot be an artifact')
    return resolved

@contextmanager
def _lock(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / '.project.lock'
    if path.is_symlink(): raise ValueError('Project lock cannot be a symlink')
    stream = path.open('a+b')
    try:
        if path.stat().st_size == 0: stream.write(b'0'); stream.flush()
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline: raise TimeoutError('Project state is locked by another operation')
                time.sleep(.025)
        try: yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally: stream.close()

def _write(root, state):
    target = root / 'project.json'
    if target.is_symlink(): raise ValueError('Project state cannot be a symlink')
    state['state_sha256'] = _hash({k: v for k, v in state.items() if k != 'state_sha256'})
    fd, temporary = tempfile.mkstemp(prefix='.project-', suffix='.tmp', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def _validate_roles(roles, source):
    if not isinstance(roles, list) or not roles: raise ValueError('At least one explicit role is required')
    ids = set()
    for role in roles:
        if not isinstance(role, dict) or not isinstance(role.get('id'), str) or not role['id'].strip():
            raise ValueError('Each role needs a nonempty string id')
        if role['id'] in ids: raise ValueError('Duplicate role identities')
        ids.add(role['id'])
        if not isinstance(role.get('animal'), str) or not role['animal'].strip(): raise ValueError('Explicit animal required')
        if not isinstance(role.get('anchors'), list): raise ValueError('Explicit anchors list required')
        indices = set()
        for anchor in role['anchors']:
            n = anchor.get('frame_index')
            if type(n) is not int or not 0 <= n < source['frame_count'] or n in indices:
                raise ValueError('Unique decoded anchor frame indices required')
            indices.add(n)
            box = anchor.get('bbox_xyxy')
            if (not isinstance(box, (list, tuple)) or len(box) != 4 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in box)):
                raise ValueError('Finite inclusive crop-relative box required')
            x, y, x2, y2 = box; _, _, w, h = source['crop_xywh']
            if not 0 <= x <= x2 < w or not 0 <= y <= y2 < h: raise ValueError('Anchor outside crop')
    return json.loads(json.dumps(roles, allow_nan=False))

def _probe(source, crop):
    sha = file_sha256(source)
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_frames',
        '-show_entries', 'stream=index,codec_type,width,height,avg_frame_rate,r_frame_rate:frame=media_type,stream_index,best_effort_timestamp_time,pts_time,duration_time,pkt_duration_time',
        '-of', 'json', str(source)], capture_output=True, text=True, encoding='utf-8', timeout=300)
    if result.returncode: raise ValueError('ffprobe could not decode source')
    raw = json.loads(result.stdout)
    video = next((s for s in raw.get('streams', []) if s.get('codec_type') == 'video'), None)
    if video is None: raise ValueError('No video stream')
    width, height = video['width'], video['height']
    crop = [0, 0, width, height] if crop is None else crop
    if (not isinstance(crop, (list, tuple)) or len(crop) != 4 or any(type(v) is not int for v in crop)
        or min(crop[:2]) < 0 or min(crop[2:]) <= 0 or crop[0]+crop[2] > width or crop[1]+crop[3] > height):
        raise ValueError('Crop requires integer nonnegative origin and positive in-bounds extent')
    decoded = [f for f in raw.get('frames', []) if f.get('media_type') == 'video' and f.get('stream_index') == video['index']]
    if not decoded: raise ValueError('Source has no decoded video frames')
    frames = []
    for i, frame in enumerate(decoded):
        pts = frame.get('best_effort_timestamp_time', frame.get('pts_time'))
        duration = frame.get('duration_time', frame.get('pkt_duration_time'))
        if pts is None: raise ValueError('Every decoded frame needs a real PTS')
        pts = float(pts)
        if not math.isfinite(pts) or (frames and pts <= frames[-1]['pts_seconds']):
            raise ValueError('Decoded PTS must be finite and strictly increasing')
        frames.append({'index': i, 'pts_seconds': pts, 'duration_seconds': float(duration) if duration else None})
    for i, frame in enumerate(frames):
        if frame['duration_seconds'] is None or frame['duration_seconds'] <= 0:
            if i+1 < len(frames): frame['duration_seconds'] = frames[i+1]['pts_seconds'] - frame['pts_seconds']
            else: raise ValueError('Last decoded frame has no actual duration')
        if not math.isfinite(frame['duration_seconds']): raise ValueError('Invalid frame duration')
    fps = float(Fraction(video.get('avg_frame_rate') or video.get('r_frame_rate')))
    if fps <= 0 or not math.isfinite(fps): raise ValueError('Video FPS metadata required')
    if file_sha256(source) != sha: raise ValueError('Source changed during inspection')
    return {'path': str(source), 'sha256': sha, 'width': width, 'height': height, 'fps': fps,
        'frame_count': len(frames), 'frames': frames, 'crop_xywh': list(crop),
        'audio_stream_count': sum(s.get('codec_type') == 'audio' for s in raw['streams'])}

def _binding(state):
    return _hash({'source': state['source'], 'roles': state['roles']})

def _load(root):
    path = root / 'project.json'
    if path.is_symlink(): raise ValueError('Project state cannot be a symlink')
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('schema') != SCHEMA: raise ValueError('Unsupported project schema')
    if state.get('state_sha256') != _hash({k: v for k, v in state.items() if k != 'state_sha256'}):
        raise ValueError('Project state was modified outside the project store')
    if state.get('binding_sha256') != _binding(state): raise ValueError('Source/role manifest binding changed')
    if file_sha256(state['source']['path']) != state['source']['sha256']: raise ValueError('Source SHA256 changed')
    return state

def init_project(source, project_dir, roles, crop_xywh=None):
    source = Path(source).expanduser().resolve(strict=True)
    if not source.is_file(): raise ValueError('A local source file is required')
    root = _root(project_dir)
    facts = _probe(source, crop_xywh)
    validated = _validate_roles(roles, facts)
    with _lock(root):
        if (root / 'project.json').exists(): raise ValueError('Project already exists; load to resume')
        state = {'schema': SCHEMA, 'project_id': str(uuid.uuid4()), 'source': facts, 'roles': validated,
            'artifacts': {}, 'events': [], 'stage': 'initialized'}
        state['binding_sha256'] = _binding(state)
        state['events'].append({'type': 'initialized', 'at': _now(), 'binding_sha256': state['binding_sha256']})
        _write(root, state)
        return state

def load_project(project_dir):
    root = _root(project_dir)
    with _lock(root): return _load(root)

def record_artifact(project_dir, name, path, status='needs_review'):
    return record_artifacts(project_dir, {name: path}, status)[name]

def _invalidate(state, names, reason):
    invalidated = []
    for name in names:
        item = state['artifacts'][name]
        if item['status'] == 'stale': continue
        item.setdefault('history', []).append({k: v for k, v in item.items() if k != 'history'})
        item.pop('acceptance', None); item['status'] = 'stale'
        item['invalidated_reason'] = reason; invalidated.append(name)
    if invalidated:
        state['events'].append({'type': 'artifacts_invalidated', 'names': sorted(invalidated), 'reason': reason, 'at': _now()})

def record_artifacts(project_dir, artifacts, status='needs_review'):
    """Publish a complete review group under one lock and one atomic replace.

    Files must already exist. Validation/replacement failure preserves the
    previous registry, allowing callers to keep rendering outputs versioned.
    """
    if not isinstance(artifacts, dict) or not artifacts: raise ValueError('Nonempty named artifact mapping required')
    if any(not isinstance(name, str) or not name.strip() for name in artifacts): raise ValueError('Artifact name required')
    if status not in ('needs_review', 'rejected'): raise ValueError('Artifacts require separate human acceptance')
    root = _root(project_dir)
    with _lock(root):
        state = _load(root); group_id = str(uuid.uuid4()); items = {}; payloads = []
        for name, candidate in artifacts.items():
            path = _internal(root, candidate)
            if path.samefile(state['source']['path']): raise ValueError('Source cannot be an output artifact')
            if path.suffix.lower() == '.json':
                payload = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(payload, dict):
                    payloads.append(payload)
                    if payload.get('source_sha256', state['source']['sha256']) != state['source']['sha256']:
                        raise ValueError('JSON artifact source SHA256 is stale')
                    if payload.get('binding_sha256', state['binding_sha256']) != state['binding_sha256']:
                        raise ValueError('JSON artifact role binding is stale')
            item = {'name': name, 'path': str(path), 'sha256': file_sha256(path), 'status': status,
                'source_sha256': state['source']['sha256'], 'binding_sha256': state['binding_sha256'],
                'group_id': group_id, 'recorded_at': _now()}
            old = state['artifacts'].get(name)
            if old: item['history'] = old.get('history', []) + [{k: v for k, v in old.items() if k != 'history'}]
            items[name] = item
        for payload in payloads:
            for dependency in ('tracks', 'observations'):
                key = dependency + '_sha256'
                if key in payload:
                    if dependency not in items and dependency not in state['artifacts']:
                        raise ValueError('JSON artifact requires registered ' + dependency)
                    parent = items[dependency] if dependency in items else _artifact(root, state, dependency)
                    if payload[key] != parent['sha256']:
                        raise ValueError('JSON artifact has stale ' + dependency + ' SHA256')
        for name, item in items.items():
            dependencies = []
            if name == 'observations': dependencies = ['tracks']
            elif name in {'cg_silent', 'cg_audio', 'comparison', 'review', 'render_report'}: dependencies = ['observations']
            if name == 'cg_silent':
                dependencies += [key for key in ('cg_audio', 'comparison', 'review', 'render_report') if key in items]
            item['depends_on'] = {}
            for dependency in dependencies:
                if dependency in items: parent = items[dependency]
                elif dependency in state['artifacts']: parent = _artifact(root, state, dependency)
                else: continue
                item['depends_on'][dependency] = parent['sha256']
        changed = {name for name, item in items.items()
            if state['artifacts'].get(name, {}).get('sha256') != item['sha256']}
        downstream = set()
        if 'tracks' in changed: downstream.update(set(state['artifacts']) - {'tracks'})
        elif 'observations' in changed: downstream.update(set(state['artifacts']) - {'tracks', 'observations'})
        elif changed & {'cg_silent', 'cg_audio', 'comparison', 'review', 'render_report'}:
            downstream.update(set(state['artifacts']) - {'tracks', 'observations'})
        _invalidate(state, downstream - set(items), 'Upstream artifact SHA256 changed: ' + ', '.join(sorted(changed)))
        state['artifacts'].update(items)
        state['events'].append({'type': 'artifacts_recorded', 'group_id': group_id, 'artifacts': items, 'at': _now()})
        state['stage'] = 'needs_review'; _write(root, state)
        return items

def _artifact(root, state, name):
    if name not in state['artifacts']: raise ValueError('Unknown artifact: ' + str(name))
    item = state['artifacts'][name]
    if item['status'] == 'stale': raise ValueError('Artifact was invalidated by an upstream change')
    path = _internal(root, item['path'])
    if path.samefile(state['source']['path']): raise ValueError('Source cannot be an output artifact')
    if item['binding_sha256'] != state['binding_sha256'] or item['source_sha256'] != state['source']['sha256']:
        raise ValueError('Artifact source/role binding is stale')
    if file_sha256(path) != item['sha256']: raise ValueError('Artifact bytes changed since review binding')
    for dependency, sha in item.get('depends_on', {}).items():
        parent = _artifact(root, state, dependency)
        if parent['sha256'] != sha: raise ValueError('Artifact dependency SHA256 is stale: ' + dependency)
    return item

def get_artifact(project_dir, name):
    root = _root(project_dir)
    with _lock(root): return _artifact(root, _load(root), name)

def accept_artifact(project_dir, name, sha256, user_confirmation):
    if not isinstance(user_confirmation, str) or not user_confirmation.strip(): raise ValueError('Explicit human confirmation required')
    root = _root(project_dir)
    with _lock(root):
        state = _load(root); item = _artifact(root, state, name)
        if sha256 != item['sha256']: raise ValueError('Reviewed SHA256 differs from current artifact')
        item['status'] = 'accepted'
        item['acceptance'] = {'sha256': sha256, 'source_sha256': state['source']['sha256'],
            'binding_sha256': state['binding_sha256'], 'group_id': item['group_id'],
            'user_confirmation': user_confirmation, 'at': _now()}
        state['events'].append({'type': 'artifact_accepted', 'name': name, **item['acceptance']})
        state['stage'] = 'accepted'; _write(root, state)
        return item

def update_roles(project_dir, roles, user_confirmation):
    if not isinstance(user_confirmation, str) or not user_confirmation.strip(): raise ValueError('Explicit role correction confirmation required')
    root = _root(project_dir)
    with _lock(root):
        state = _load(root); validated = _validate_roles(roles, state['source'])
        previous = state['roles']; state['roles'] = validated; state['binding_sha256'] = _binding(state)
        _invalidate(state, set(state['artifacts']), 'Reviewed role/anchor correction')
        state['events'].append({'type': 'roles_updated', 'previous_roles': previous, 'roles': validated,
            'user_confirmation': user_confirmation, 'binding_sha256': state['binding_sha256'], 'at': _now()})
        state['stage'] = 'initialized'; _write(root, state)
        return state
