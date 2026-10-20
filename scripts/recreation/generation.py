"""Independent, opt-in CG generation. No production config, environment keys or retries.

prepare -> approve -> upload (or verify existing HTTPS) -> submit -> status -> download.
Only prepare/approve are local. Every network action requires explicit credentials.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone, timedelta
from fractions import Fraction
import hashlib
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
import uuid

ENDPOINT = 'https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks'
SKILL_ROOT = Path(__file__).resolve().parents[2]
DOCS = 'https://docs.volcengine.com/docs/ark/'
PROFILES = {
    'doubao-seedance-2-0-260128': {'family': '2.0', 'max_video_seconds': 15,
        'output_resolutions': ['480p', '720p'], 'output_formats': ['mp4'], 'generate_audio': False},
    'doubao-seedance-2-0-fast-260128': {'family': '2.0', 'max_video_seconds': 15,
        'output_resolutions': ['480p', '720p'], 'output_formats': ['mp4'], 'generate_audio': False},
    'doubao-seedance-2-0-mini-260615': {'family': '2.0', 'max_video_seconds': 15,
        'output_resolutions': ['480p', '720p'], 'output_formats': ['mp4'], 'generate_audio': False},
    'doubao-seedance-2-5-260628': {'family': '2.5', 'max_video_seconds': 30,
        'output_resolutions': ['480p', '720p'], 'output_formats': ['mp4', 'mov'], 'generate_audio': False},
}


def _profile(model):
    if model not in PROFILES:
        raise ValueError('Unsupported model profile; no automatic fallback')
    return {**PROFILES[model], 'model': model, 'adapter_version': 1, 'verified_at': '2026-10-20',
            'sources': [DOCS+'seedance-2-0?lang=zh', DOCS+'seedance-2-5?lang=zh',
                        DOCS+'create-video-generation-task-api?lang=zh']}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path if exclusive else path.with_name('.g-' + uuid.uuid4().hex[:12] + '.tmp')
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError('An attempt or immutable record already exists; never repeat a mutation') from None
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    if not exclusive:
        os.replace(target, path)


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {redact(str(k), secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            value = value.replace(str(secret), '[redacted]')
    value = re.sub(r'https?://[^\s\"\'<>]+|data:[^\s\"\'<>]+', '[URL redacted]', value, flags=re.IGNORECASE)
    return value[:1500]


def _future(value, margin=0):
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return timestamp.tzinfo is not None and timestamp > datetime.now(timezone.utc) + timedelta(seconds=margin)
    except (TypeError, ValueError, AttributeError):
        return False


def _positive_int(value):
    return type(value) is int and value > 0


def _scrub_secrets(value, credentials):
    """Retain signed output URL privately, while never persisting provider API keys."""
    tokens = [credentials[k] for k in ('ARK_API_KEY', 'VOLC_AK', 'VOLC_SK') if credentials.get(k)]
    if isinstance(value, dict):
        return {_scrub_secrets(str(k), credentials): _scrub_secrets(v, credentials) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_secrets(v, credentials) for v in value]
    if isinstance(value, str):
        for token in tokens:
            value = value.replace(token, '[redacted]')
    return value


def _https(url):
    if not isinstance(url, str):
        raise ValueError('A secure HTTPS URL is required')
    u = urlsplit(url)
    if (u.scheme != 'https' or not u.hostname or u.username or u.password or u.fragment
            or u.port not in (None, 443) or any(ord(c) < 33 for c in url)):
        raise ValueError('A secure HTTPS URL without credentials, fragment or custom port is required')
    return u


def _external_file(path):
    if not path:
        raise ValueError('An explicit external JSON file is required; environment and .env are never read')
    p = Path(path).expanduser().resolve(strict=True)
    if p == SKILL_ROOT or SKILL_ROOT in p.parents or p.suffix.lower() != '.json':
        raise ValueError('Configuration must be JSON outside the replaceable Skill')
    return p


def load_credentials(path):
    data = read_json(_external_file(path))
    if not all(isinstance(data.get(k), str) and data[k].strip() for k in ('ARK_API_KEY', 'account_id', 'region')):
        raise ValueError('Explicit ARK_API_KEY, account_id and region are required')
    allowed = {'ARK_API_KEY', 'account_id', 'region', 'VOLC_AK', 'VOLC_SK', 'TOS_BUCKET', 'TOS_REGION', 'TOS_ENDPOINT'}
    if set(data) - allowed:
        raise ValueError('Unknown credential fields; old pipeline configurations are not accepted')
    return data


def _get_artifact(project_dir, name):
    # Lazy local-only import; this adapter has no management/pipeline dependency.
    try:
        from .project_store import get_artifact
    except ImportError:
        from project_store import get_artifact
    return get_artifact(project_dir, name)


def _accepted(project_dir, name):
    a = _get_artifact(project_dir, name)
    p = Path(a['path']).resolve(strict=True)
    project = Path(project_dir).resolve(strict=True)
    acceptance = a.get('acceptance') or {}
    if (project not in p.parents or a.get('status') != 'accepted' or not acceptance.get('user_confirmation')
            or acceptance.get('sha256') != a.get('sha256') or sha(p) != a.get('sha256')):
        raise ValueError('Current accepted artifact and real SHA are required')
    return {'name': name, 'path': str(p), 'sha256': a['sha256'],
            'binding_sha256': a['binding_sha256'], 'source_sha256': a['source_sha256']}


def probe_media(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-show_format', '-show_frames',
                             '-of', 'json', str(path)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=300)
    if result.returncode:
        raise ValueError('ffprobe failed; no media facts are invented')
    raw = json.loads(result.stdout)
    videos = [s for s in raw.get('streams', []) if s.get('codec_type') == 'video']
    if len(videos) != 1:
        raise ValueError('Exactly one video stream is required')
    v = videos[0]
    video = {'width': int(v['width']), 'height': int(v['height']),
             'fps': float(Fraction(v['avg_frame_rate'])),
             'duration': float(v.get('duration') or raw.get('format', {})['duration']),
             'frame_count': int(v['nb_read_frames']), 'codec_name': v.get('codec_name'),
             'time_base': v.get('time_base')}
    if not all(math.isfinite(video[k]) and video[k] > 0 for k in ('fps', 'duration', 'frame_count')):
        raise ValueError('Finite decoded video facts are required')
    frames = []
    for frame in raw.get('frames', []):
        if frame.get('media_type') != 'video' or frame.get('stream_index') != v['index']:
            continue
        timestamp = frame.get('best_effort_timestamp_time', frame.get('pts_time'))
        duration = frame.get('duration_time', frame.get('pkt_duration_time'))
        frames.append({'pts_seconds': float(timestamp) if timestamp is not None else None,
                       'duration_seconds': float(duration) if duration is not None else None})
    return {'video': video, 'frames': frames, 'audio_stream_count': sum(s.get('codec_type') == 'audio' for s in raw['streams']),
            'ffprobe': raw}


def _image_facts(path):
    """Decode through ffprobe rather than trusting a renamed file's suffix."""
    p = Path(path)
    with p.open('rb') as f:
        header = f.read(16)
    mime = ('image/png' if header.startswith(b'\x89PNG\r\n\x1a\n') else
            'image/jpeg' if header.startswith(b'\xff\xd8\xff') else None)
    if not mime or (mime == 'image/png') != (p.suffix.lower() == '.png'):
        raise ValueError('Identity image bytes and PNG/JPEG extension must agree')
    result = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-of', 'json', str(p)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    if result.returncode:
        raise ValueError('Identity image decode failed')
    streams = json.loads(result.stdout).get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_type') != 'video':
        raise ValueError('Identity image must contain a single still image')
    v = streams[0]
    # A deliberately conservative subset, not the full provider image capability.
    if (v.get('nb_read_frames') != '1' or not 300 <= v['width'] <= 4096
            or not 300 <= v['height'] <= 4096 or not 0.4 <= v['width']/v['height'] <= 2.5):
        raise ValueError('Static identity image must fit the adapter image subset: 300..4096px and ratio 0.4..2.5')
    return mime


def compile_parameters(model, options, probe):
    if model not in PROFILES:
        raise ValueError('Unsupported model profile; no paid permission probing or automatic model substitution')
    if set(options) - {'duration', 'resolution', 'output_format', 'watermark'}:
        raise ValueError('Unsupported profile parameter')
    profile = PROFILES[model]
    v = probe['video']
    seconds = v['duration']
    minimum = 4 if profile['family'] == '2.5' else 2
    if (probe['audio_stream_count'] != 0 or not minimum <= seconds <= profile['max_video_seconds']
            or not 24 <= v['fps'] <= 60 or v['codec_name'] not in ('h264', 'hevc')
            or not 300 <= v['width'] <= 6000 or not 300 <= v['height'] <= 6000
            or not 0.4 <= v['width'] / v['height'] <= 2.5
            or not 407696 <= v['width'] * v['height'] <= 8295044):
        raise ValueError('CG must be silent and meet the documented video input constraints')
    resolution = options.get('resolution')
    if resolution not in profile['output_resolutions']:
        raise ValueError('Select an explicitly supported 480p or 720p output; this adapter does not compile other variants')
    duration = options.get('duration')
    if type(duration) is not int or (profile['family'] == '2.5' and duration != -1):
        raise ValueError('2.5 edit requires duration=-1; duration must be explicitly selected')
    if profile['family'] == '2.0' and duration != -1 and not 4 <= duration <= 15:
        raise ValueError('2.0 duration must be -1 or an integer in [4,15]; -1 does not guarantee input duration')
    if 'watermark' in options and type(options['watermark']) is not bool:
        raise ValueError('watermark must be boolean')
    p = {'model': model, 'duration': duration, 'resolution': resolution, 'ratio': 'adaptive',
         'generate_audio': False, 'watermark': options.get('watermark', False)}
    if profile['family'] == '2.5':
        fmt = options.get('output_format', 'mp4')
        if fmt not in profile['output_formats']:
            raise ValueError('2.5 supports mp4 or mov output')
        p.update(omni_reference_task_type='edit', output_format=fmt)
    elif options.get('output_format', 'mp4') != 'mp4':
        raise ValueError('2.0 output is mp4; output_format is a 2.5-only API parameter')
    return p


def _run_dir(project_dir, run_id):
    project = Path(project_dir).resolve(strict=True)
    if not re.fullmatch(r'[a-f0-9]{32}', str(run_id)):
        raise ValueError('Invalid generation run ID')
    p = project / 'generation' / run_id
    if p.resolve() != p or project not in p.resolve().parents:
        raise ValueError('Generation journal path must remain inside the project without symlinks')
    return p


def _validate_output_paths(directory, platform=None):
    """Refuse known Windows MAX_PATH failures before accepting or spending."""
    if (platform or os.name) != 'nt':
        return
    names = ['plan.json', 'approval.json', 'upload-journal.json', 'submit-journal.json',
             'reference-private.json', 'provider-response-private.json', 'model-original.bin',
             'download.json', '.g-xxxxxxxxxxxx.tmp',
             'delivery/20000101T000000-xxxxxxxx/model-original-audio.mp4',
             'delivery/20000101T000000-xxxxxxxx/comparison.mp4',
             'delivery/20000101T000000-xxxxxxxx/review.html',
             'delivery/20000101T000000-xxxxxxxx/report.json']
    if any(len(str(Path(directory)/name).encode('utf-16-le'))//2 > 259 for name in names):
        raise ValueError('Generation output paths exceed the portable Windows path limit; '
                         'choose a shorter external project directory before preparing/approving or uploading')


def _price(pricing, model, budget_cents):
    allowed = {'model', 'currency', 'upper_cost_cents', 'basis', 'source_url', 'checked_at', 'valid_until',
               'account_id', 'region', 'resolution', 'covered_output_seconds', 'includes_reference_video_cost'}
    if set(pricing) - allowed:
        raise ValueError('Unknown price fields; use an independent quote, never a full pipeline configuration')
    if (pricing.get('model') != model or pricing.get('currency') != 'CNY'
            or not _positive_int(pricing.get('upper_cost_cents')) or not _positive_int(budget_cents)
            or pricing['upper_cost_cents'] > budget_cents or not _future(pricing.get('valid_until'))
            or not isinstance(pricing.get('basis'), str) or len(pricing['basis'].strip()) < 12
            or not pricing.get('checked_at')):
        raise ValueError('Current documented upper cost estimate must fit the explicit budget')
    _https(pricing.get('source_url'))
    try:
        checked = datetime.fromisoformat(pricing['checked_at'].replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - checked).total_seconds()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Price basis requires a timezone-aware current checked_at') from None
    if not -60 <= age <= 86400:
        raise ValueError('Price basis must have been checked within the current 24 hours')


def _price_coverage(pricing, account, parameters, probe):
    duration = parameters['duration']
    bound = (probe['video']['duration'] if PROFILES[parameters['model']]['family'] == '2.5'
             else 15 if duration == -1 else duration)
    coverage = pricing.get('covered_output_seconds')
    if (any(pricing.get(k) != account.get(k) for k in ('account_id', 'region'))
            or pricing.get('resolution') != parameters['resolution']
            or pricing.get('includes_reference_video_cost') is not True
            or type(coverage) not in (int, float) or not math.isfinite(coverage) or coverage < bound):
        raise ValueError('Price estimate must cover this account, region, resolution, reference video and maximum output window')


def prepare(project_dir, *, model, prompt, account, pricing, budget_cents, options, image_artifacts=()):
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 10000
            or not any(k in prompt.lower() for k in ('编辑', '修改', '替换', '改成', 'edit', 'replace', 'modify'))):
        raise ValueError('An explicit video editing prompt is required')
    if set(account) - {'account_id', 'region', 'enabled_models', 'entitlement_evidence'}:
        raise ValueError('Account selection contains unknown fields; never put credentials in a frozen plan')
    if (account.get('region') != 'cn-beijing' or not account.get('account_id')
            or model not in account.get('enabled_models', []) or not account.get('entitlement_evidence')):
        raise ValueError('Selected account/region and human-verified model entitlement are required')
    _price(pricing, model, budget_cents)
    cg = _accepted(project_dir, 'cg_silent')
    probe = probe_media(cg['path'])
    if Path(cg['path']).suffix.lower() not in ('.mp4', '.mov') or Path(cg['path']).stat().st_size > 200 * 1024**2:
        raise ValueError('CG container must be MP4/MOV and at most 200 MB')
    params = compile_parameters(model, options, probe)
    _price_coverage(pricing, account, params, probe)
    images = []
    if len(image_artifacts) > 9 or len(set(image_artifacts)) != len(image_artifacts):
        raise ValueError('At most 9 distinct synthetic identity image artifacts are supported')
    for name in image_artifacts:
        if not re.fullmatch(r'identity_[a-zA-Z0-9_-]+', name):
            raise ValueError('Only explicitly accepted identity_* synthetic image artifacts may be included')
        image = _accepted(project_dir, name)
        p = Path(image['path'])
        if p.suffix.lower() not in ('.png', '.jpg', '.jpeg') or p.stat().st_size > 10 * 1024**2:
            raise ValueError('Identity images must be PNG/JPEG and at most 10 MB')
        image['mime'] = _image_facts(p)
        images.append(image)
    run_id = uuid.uuid4().hex
    plan = {'schema': 'recreation-generation-plan-v1', 'run_id': run_id, 'created_at': now(),
            'account': account, 'pricing': pricing, 'budget_cents': budget_cents,
            'max_paid_generations': 1, 'automatic_paid_retries': 0, 'cg': cg, 'images': images,
            'cg_probe': probe, 'prompt': prompt, 'parameters': params,
            'profile': _profile(model),
            'upload_original_pixels': False, 'upload_original_audio': False}
    path = _run_dir(project_dir, run_id)
    _validate_output_paths(path)
    path.mkdir(parents=True, exist_ok=False)
    write_json(path / 'plan.json', plan, exclusive=True)
    return {'run_id': run_id, 'plan_sha256': fingerprint(plan), 'plan_path': str(path/'plan.json'),
            'status': 'prepared_requires_current_user_confirmation',
            'model': model, 'budget_cents': budget_cents, 'max_paid_generations': 1, 'uploads': 0, 'paid_calls': 0}


def _validate(project_dir, run_id, credentials=None, approval=True):
    directory = _run_dir(project_dir, run_id)
    _validate_output_paths(directory)
    plan = read_json(directory / 'plan.json')
    if plan.get('schema') != 'recreation-generation-plan-v1' or plan.get('run_id') != run_id:
        raise ValueError('Invalid frozen plan')
    _price(plan['pricing'], plan['parameters']['model'], plan['budget_cents'])
    if plan.get('profile') != _profile(plan['parameters']['model']):
        raise ValueError('Adapter profile changed; prepare and approve a new frozen plan')
    if plan.get('max_paid_generations') != 1 or plan.get('automatic_paid_retries') != 0:
        raise ValueError('Only one attempt with zero automatic retries is supported')
    for record in [plan['cg']] + plan['images']:
        current = _accepted(project_dir, record['name'])
        if any(current[k] != record[k] for k in ('path', 'sha256', 'binding_sha256', 'source_sha256')):
            raise ValueError('Project or accepted inputs changed; prepare and obtain new approval')
    # Probe real current bytes again, including an explicit zero-audio check.
    fresh = probe_media(plan['cg']['path'])
    _price_coverage(plan['pricing'], plan['account'], plan['parameters'], fresh)
    opts = {k: v for k, v in plan['parameters'].items() if k in ('resolution', 'duration', 'output_format', 'watermark')}
    if compile_parameters(plan['parameters']['model'], opts, fresh) != plan['parameters']:
        raise ValueError('Profile or media facts changed')
    if approval:
        a = read_json(directory / 'approval.json') if (directory / 'approval.json').is_file() else {}
        if (a.get('plan_sha256') != fingerprint(plan) or a.get('run_id') != run_id
                or a.get('synthetic_inputs_only') is not True or not a.get('user_confirmation')
                or a.get('approved') is not True):
            raise ValueError('Current explicit user approval for this frozen model, inputs and budget is required')
    if credentials is not None:
        if (not credentials.get('ARK_API_KEY') or any(credentials.get(k) != plan['account'].get(k)
                                                     for k in ('account_id', 'region'))):
            raise ValueError('Explicit credentials must belong to the selected approved account and region')
    return directory, plan


def approve(project_dir, run_id, plan_sha256, user_confirmation, *, synthetic_inputs_only=False):
    directory, plan = _validate(project_dir, run_id, approval=False)
    if (plan_sha256 != fingerprint(plan) or not isinstance(user_confirmation, str)
            or len(user_confirmation.strip()) < 4 or synthetic_inputs_only is not True):
        raise ValueError('Approval must bind the exact displayed plan and a current explicit user statement')
    write_json(directory / 'approval.json', {'schema': 'recreation-generation-approval-v1', 'approved': True,
               'run_id': run_id, 'plan_sha256': plan_sha256, 'user_confirmation': user_confirmation,
               'synthetic_inputs_only': True, 'approved_at': now()}, exclusive=True)
    return {'status': 'approved_upload_and_one_generation', 'run_id': run_id, 'plan_sha256': plan_sha256}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirects are not permitted')


class Transport:
    """No proxy inheritance, redirects or retry. Secrets are never logged."""
    @staticmethod
    def _open(request):
        u = _https(request.full_url)
        # Fail closed on private/reserved endpoints, including hostnames resolving locally.
        addresses = socket.getaddrinfo(u.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Only public HTTPS endpoints are permitted')
        return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=90)

    def get_bytes(self, url, limit):
        with self._open(Request(url, headers={'Accept-Encoding': 'identity'})) as response:
            if response.status != 200:
                raise ValueError('Media GET must return 200')
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError('Media exceeds the approved size limit')
            return data

    def request_json(self, method, url, key, payload=None):
        if method not in ('POST', 'GET') or not (url == ENDPOINT or url.startswith(ENDPOINT + '/')):
            raise ValueError('Unsupported provider request')
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        request = Request(url, data=data, method=method,
                          headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            with self._open(request) as r:
                body = r.read(2 * 1024**2 + 1)
                if len(body) > 2 * 1024**2:
                    raise ValueError('Provider response exceeds limit')
                return r.status, json.loads(body)
        except HTTPError as exc:
            try:
                body = json.loads(exc.read(2 * 1024**2))
            except (ValueError, UnicodeError):
                body = {}
            return exc.code, body


def _verify_remote(transport, url, record):
    _https(url)
    data = transport.get_bytes(url, Path(record['path']).stat().st_size)
    if hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ValueError('Remote reference bytes do not match the approved synthetic CG SHA256')
    return len(data)


def upload(project_dir, run_id, credentials, transport=None, *, reference=None):
    """Verify existing HTTPS CG, or perform at most one private TOS PUT for this run."""
    transport = transport or Transport()
    directory, plan = _validate(project_dir, run_id, credentials)
    journal = directory / 'upload-journal.json'
    if journal.exists():
        raise ValueError('Prior upload/verification attempt exists; use verify-upload for GET-only recovery')
    if reference is None:
        _tos_preflight(credentials)  # Missing local SDK/config cannot consume a PUT reservation.
    state = {'status': 'verifying_existing' if reference is not None else 'put_in_flight', 'run_id': run_id,
             'plan_sha256': fingerprint(plan), 'cg_sha256': plan['cg']['sha256'],
             'put_attempts': 0 if reference is not None else 1, 'started_at': now()}
    if reference is None:
        state['object_key'] = 'recreation/' + run_id + '/' + plan['cg']['sha256'] + Path(plan['cg']['path']).suffix.lower()
    write_json(journal, state, exclusive=True)  # Before any PUT; no retry even after process crash.
    try:
        if reference is None:
            reference = _tos_reference(credentials, state, plan, put=True)
        _https(reference['url'])
        if not _future(reference.get('expires_at'), 600):
            raise ValueError('Reference URL needs at least ten minutes of remaining lifetime')
        total = _verify_remote(transport, reference['url'], plan['cg'])
        write_json(directory / 'reference-private.json', {**reference, 'sha256': plan['cg']['sha256'],
                   'verified_bytes': total, 'plan_sha256': fingerprint(plan)})
        state.update(status='remote_verified', verified_remote_sha256=plan['cg']['sha256'])
    except Exception as exc:
        state.update(status='ambiguous_PUT_or_reference_unverified_GET_only', error_type=type(exc).__name__)
        write_json(journal, state)
        raise ValueError('Upload outcome uncertain or verification failed; GET-only verify-upload, never repeat PUT') from None
    write_json(journal, state)
    return state


def _tos_preflight(credentials):
    required = ('VOLC_AK', 'VOLC_SK', 'TOS_BUCKET', 'TOS_ENDPOINT', 'TOS_REGION')
    if not all(credentials.get(k) for k in required):
        raise ValueError('Explicit TOS configuration required; there are no storage defaults')
    endpoint = credentials['TOS_ENDPOINT'].removeprefix('https://')
    if (not re.fullmatch(r'tos-cn-[a-z0-9-]+\.volces\.com', endpoint)
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', credentials['TOS_BUCKET'])
            or credentials['TOS_REGION'] != credentials['region']
            or endpoint != 'tos-' + credentials['TOS_REGION'] + '.volces.com'):
        raise ValueError('Invalid or mismatched TOS region/endpoint')
    import tos  # Optional generate-tos stage only. No SDK imports during local CG work.
    return tos


def _tos_reference(credentials, state, plan, *, put):
    tos = _tos_preflight(credentials)
    endpoint = credentials['TOS_ENDPOINT'].removeprefix('https://')
    client = tos.TosClientV2(credentials['VOLC_AK'], credentials['VOLC_SK'],
        endpoint='https://' + endpoint, region=credentials['TOS_REGION'],
        enable_verify_ssl=True, max_retry_count=0, follow_redirect_times=0)
    bucket, key = credentials['TOS_BUCKET'], state['object_key']
    if put:
        data = Path(plan['cg']['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != plan['cg']['sha256']:
            raise ValueError('CG changed before PUT')
        client.put_object(bucket, key, content=io.BytesIO(data), content_length=len(data),
            content_type='video/mp4' if key.endswith('.mp4') else 'video/quicktime',
            acl=tos.ACLType.ACL_Private, forbid_overwrite=True)
    url = client.pre_signed_url(tos.HttpMethodType.Http_Method_Get, bucket, key, expires=3600).signed_url
    u = _https(url)
    if u.hostname != bucket + '.' + endpoint:
        raise ValueError('Unexpected signed media host')
    return {'url': url, 'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}


def verify_upload(project_dir, run_id, credentials, transport=None, *, reference=None):
    """Read existing object only; never invokes PUT, including after an ambiguous upload."""
    transport = transport or Transport()
    directory, plan = _validate(project_dir, run_id, credentials)
    state = read_json(directory / 'upload-journal.json')
    if state.get('plan_sha256') != fingerprint(plan):
        raise ValueError('Upload journal belongs to another plan')
    if reference is None:
        reference = (_tos_reference(credentials, state, plan, put=False) if state.get('object_key')
                     else read_json(directory / 'reference-private.json'))
    if not _future(reference.get('expires_at'), 600):
        raise ValueError('Reference expired; supply a renewed URL to the same exact approved bytes')
    total = _verify_remote(transport, reference['url'], plan['cg'])
    write_json(directory / 'reference-private.json', {**reference, 'sha256': plan['cg']['sha256'],
               'verified_bytes': total, 'plan_sha256': fingerprint(plan)})
    state.update(status='remote_verified', verified_remote_sha256=plan['cg']['sha256'])
    write_json(directory / 'upload-journal.json', state)
    return state


def submit(project_dir, run_id, credentials, transport=None):
    transport = transport or Transport()
    directory, plan = _validate(project_dir, run_id, credentials)
    journal = directory / 'submit-journal.json'
    if journal.exists():
        raise ValueError('Prior POST attempt exists: only status/download GET recovery is permitted')
    ref = read_json(directory / 'reference-private.json')
    if (ref.get('sha256') != plan['cg']['sha256'] or ref.get('plan_sha256') != fingerprint(plan)
            or not _future(ref.get('expires_at'), 600)):
        raise ValueError('Current verified reference with enough remaining lifetime is required')
    _verify_remote(transport, ref['url'], plan['cg'])  # Verify real bytes at the actual model reference again.
    content = [{'type': 'text', 'text': plan['prompt']}]
    for image in plan['images']:
        data = Path(image['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != image['sha256']:
            raise ValueError('Identity image changed before submission')
        content.append({'type': 'image_url', 'role': 'reference_image', 'image_url': {
            'url': 'data:' + image['mime'] + ';base64,' + base64.b64encode(data).decode('ascii')}})
    content.append({'type': 'video_url', 'role': 'reference_video', 'video_url': {'url': ref['url']}})
    payload = {**plan['parameters'], 'content': content}
    _validate(project_dir, run_id, credentials)  # Long remote GET must not bypass stale local inputs/prices.
    state = {'status': 'post_in_flight', 'run_id': run_id, 'plan_sha256': fingerprint(plan),
             'request_sha256': fingerprint(payload), 'budget_cents': plan['budget_cents'],
             'max_paid_generations': 1, 'post_attempts': 1, 'automatic_paid_retries': 0,
             'task_id': None, 'started_at': now()}
    write_json(journal, state, exclusive=True)  # Exclusive durable reservation before POST; concurrent callers lose here.
    try:
        code, data = transport.request_json('POST', ENDPOINT, credentials['ARK_API_KEY'], payload)
        state['http_status'] = code
        task = data.get('id') if isinstance(data, dict) else None
        if 200 <= code < 300 and isinstance(task, str) and re.fullmatch(r'cgt-[A-Za-z0-9._-]+', task):
            state.update(status='submitted_GET_only', task_id=task)
        else:
            state['status'] = 'rejected_no_retry' if 400 <= code < 500 else 'ambiguous_GET_only'
            state['provider_error'] = redact(data.get('error', {}) if isinstance(data, dict) else {}, credentials.values())
    except Exception as exc:
        state.update(status='ambiguous_GET_only', error_type=type(exc).__name__)
    write_json(journal, state)
    return state


def status(project_dir, run_id, credentials=None, transport=None):
    directory = _run_dir(project_dir, run_id)
    state = read_json(directory / 'submit-journal.json') if (directory / 'submit-journal.json').exists() else {
        'status': 'not_submitted', 'run_id': run_id, 'paid_calls': 0}
    if credentials is None:
        return state
    # GET recovery remains available after stale CG or expired estimates: no mutation can be made here.
    plan = read_json(directory / 'plan.json')
    if any(credentials.get(k) != plan['account'].get(k) for k in ('account_id', 'region')) or not credentials.get('ARK_API_KEY'):
        raise ValueError('Selected approved account credentials are required')
    task = state.get('task_id')
    if not isinstance(task, str) or not re.fullmatch(r'cgt-[A-Za-z0-9._-]+', task):
        raise ValueError('No known task ID; stop and reconcile provider console. Never repeat POST')
    code, data = (transport or Transport()).request_json('GET', ENDPOINT + '/' + task, credentials['ARK_API_KEY'])
    state.update(last_get_http_status=code, last_checked_at=now())
    if code == 200:
        if not isinstance(data, dict) or data.get('id') != task:
            raise ValueError('Provider returned a different task identity')
        write_json(directory / 'provider-response-private.json', _scrub_secrets(data, credentials))
        state['status'] = 'provider_' + str(data.get('status'))
        state['provider_status'] = data.get('status')
        state['provider_error'] = redact(data.get('error', {}), credentials.values())
        state['usage'] = {k: v for k, v in (data.get('usage') or {}).items() if type(v) in (int, float)}
        for key in ('duration', 'frames', 'framespersecond', 'resolution', 'ratio', 'seed'):
            if type(data.get(key)) in (int, float, str):
                state['provider_' + key] = redact(data[key], credentials.values())
    write_json(directory / 'submit-journal.json', state)
    return state


def download(project_dir, run_id, credentials, transport=None):
    transport = transport or Transport()
    state = status(project_dir, run_id, credentials, transport)
    if state.get('provider_status') != 'succeeded':
        raise ValueError('Provider task has not succeeded; no new generation is attempted')
    directory = _run_dir(project_dir, run_id)
    data = read_json(directory / 'provider-response-private.json')
    url = (data.get('content') or {}).get('video_url')
    _https(url)
    output = directory / 'model-original.bin'
    report = directory / 'download.json'
    if output.exists():
        previous = read_json(report) if report.exists() else {}
        if previous.get('sha256') != sha(output):
            raise ValueError('Existing output needs manual reconciliation; never overwrite')
        return previous
    blob = transport.get_bytes(url, 512 * 1024**2)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    # Native bytes remain untouched, even if probe fails. Never loop/freeze/retime to fill input duration.
    try:
        probe = probe_media(output)
    except Exception:
        write_json(report, {'status': 'downloaded_probe_failed', 'path': str(output), 'sha256': sha(output),
                            'bytes': len(blob), 'visual_quality_accepted': False})
        raise ValueError('Original downloaded, but ffprobe failed; inspect local original without resubmission') from None
    result = {'status': 'downloaded_original', 'path': str(output), 'sha256': sha(output), 'bytes': len(blob),
              'probe': probe, 'visual_quality_accepted': False, 'duration_padding_applied': False}
    write_json(report, result, exclusive=True)
    try:
        result['delivery'] = package_download(project_dir, run_id)
        write_json(report, result)
    except Exception as exc:
        result['delivery'] = {'status': 'local_packaging_failed_original_preserved', 'error_type': type(exc).__name__,
                              'next_action': 'Inspect local inputs; package can be retried locally without another cloud request'}
        write_json(report, result)
    return result


def package_download(project_dir, run_id):
    """Pure local source-audio/comparison/review packaging; never calls a transport."""
    directory = _run_dir(project_dir, run_id)
    result = read_json(directory / 'download.json')
    output = directory / 'model-original.bin'
    if result.get('sha256') != sha(output):
        raise ValueError('Native downloaded bytes changed; packaging must not overwrite or fabricate a result')
    try:
        from .generation_package import package_native
    except ImportError:
        from generation_package import package_native
    return package_native(project_dir, directory, read_json(directory/'plan.json'), output, probe_media(output))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    q = sub.add_parser('prepare')
    q.add_argument('project', type=Path)
    q.add_argument('--model', choices=list(PROFILES), required=True)
    q.add_argument('--prompt-file', type=Path, required=True)
    q.add_argument('--account-file', type=Path, required=True)
    q.add_argument('--pricing-file', type=Path, required=True)
    q.add_argument('--budget-cents', type=int, required=True)
    q.add_argument('--duration', type=int, required=True)
    q.add_argument('--resolution', choices=['480p', '720p'], required=True)
    q.add_argument('--output-format', choices=['mp4', 'mov'], default='mp4')
    q.add_argument('--image-artifact', action='append', default=[])
    q = sub.add_parser('approve')
    q.add_argument('project', type=Path)
    q.add_argument('run_id')
    q.add_argument('--plan-sha256', required=True)
    q.add_argument('--user-confirmation', required=True)
    q.add_argument('--synthetic-inputs-only', action='store_true', required=True)
    for action in ('upload', 'verify-upload', 'submit', 'status', 'download', 'package'):
        q = sub.add_parser(action)
        q.add_argument('project', type=Path)
        q.add_argument('run_id')
        if action != 'package':
            q.add_argument('--credentials-file', type=Path, required=action != 'status')
        if action in ('upload', 'verify-upload'):
            q.add_argument('--reference-config-file', type=Path)
    args = p.parse_args(argv)
    secrets = ()
    try:
        if args.action == 'prepare':
            result = prepare(args.project, model=args.model, prompt=args.prompt_file.read_text(encoding='utf-8'),
                account=read_json(_external_file(args.account_file)), pricing=read_json(_external_file(args.pricing_file)),
                budget_cents=args.budget_cents, options={'duration': args.duration, 'resolution': args.resolution,
                'output_format': args.output_format}, image_artifacts=args.image_artifact)
        elif args.action == 'approve':
            result = approve(args.project, args.run_id, args.plan_sha256, args.user_confirmation,
                             synthetic_inputs_only=args.synthetic_inputs_only)
        elif args.action == 'package':
            result = package_download(args.project, args.run_id)
        else:
            creds = load_credentials(args.credentials_file) if args.credentials_file else None
            secrets = tuple(creds.values()) if creds else ()
            if args.action in ('upload', 'verify-upload'):
                ref = read_json(_external_file(args.reference_config_file)) if args.reference_config_file else None
                result = (upload if args.action == 'upload' else verify_upload)(args.project, args.run_id, creds, reference=ref)
            else:
                result = {'submit': submit, 'status': status, 'download': download}[args.action](args.project, args.run_id, creds)
        # URLs/credentials in provider errors or raw probe tags are never printed to Agent output.
        print(json.dumps(redact(result, secrets), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'stopped_no_automatic_retry', 'error_type': type(exc).__name__,
                          'detail': redact(str(exc), secrets) if isinstance(exc, ValueError)
                          else 'Inspect local journal; never repeat PUT/POST'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
