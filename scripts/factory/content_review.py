"""Local, server-controlled content authority and immutable render bindings.

Model diagnostics are always automatic. Only the explicit management command
may persist a human record in the top-level QA content_reviews collection.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
from copy import deepcopy

from . import visual_contract, visual_evidence
from .review_candidates import controlled_candidate_path, controlled_job_asset_path

AUTHORITY = 'management_content_review_v1'
STATUSES = {'satisfied', 'conflicting', 'insufficient_evidence', 'not_applicable'}
LOCAL_SETTINGS = ('asr_enabled', 'hook_overlay_enabled')


def object_json(raw):
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def requirements(master):
    return visual_contract.normalize_requirements(master.get('visual_requirements') or
        (master.get('visual_plan') or {}).get('visual_requirements') or [])


def enabled(storyboard):
    return bool(storyboard.get('visual_requirements') or any(m.get('visual_requirements') or
        (m.get('visual_plan') or {}).get('visual_requirements') for m in storyboard.get('masters') or []))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def contract_hash(storyboard):
    return digest({'root': visual_contract.normalize_requirements(storyboard.get('visual_requirements') or []),
                   'masters': [{'idx': m['idx'], 'requirements': requirements(m)} for m in storyboard.get('masters') or []]})


def validate_contract(storyboard):
    root = {r['id']: r for r in visual_contract.normalize_requirements(storyboard.get('visual_requirements') or [])}
    declared = [r for m in storyboard.get('masters') or [] for r in requirements(m)]
    if enabled(storyboard) and (not root or any(root.get(r['id']) != r for r in declared)
        or set(root) - {r['id'] for r in declared}):
        raise ValueError('content contract root/master definitions are inconsistent')


def edit_plan_fingerprint(storyboard):
    # Include every persisted edit field: picture/audio clocks, transitions,
    # subtitle cues and actual compose settings, including environment CTA.
    return digest({'mode': storyboard.get('production_mode'), 'shots': storyboard.get('shots'),
        'settings': {key: (storyboard.get('production_settings') or {}).get(key) for key in LOCAL_SETTINGS}, 'hook': storyboard.get('hook'),
        'append_cta': os.getenv('APPEND_CTA', 'true').lower() == 'true', 'bgm_db': -12.0})


def effective_storyboard(live, actual):
    """Project the actual local edit onto live metadata, preserving paid inputs.

    Frozen graph input remains immutable. The persisted job owns the effective
    picture/audio timeline used by retention, approval and later local recuts.
    """
    validate_contract(live); validate_contract(actual)
    if contract_hash(live) != contract_hash(actual):
        raise ValueError('content current requirements differ from frozen local compose input')
    merged = deepcopy(live)
    for field in ('production_mode', 'shots', 'hook'):
        if field in actual:
            merged[field] = deepcopy(actual[field])
        else:
            merged.pop(field, None)
    settings = merged.setdefault('production_settings', {})
    for field in LOCAL_SETTINGS:
        if field in (actual.get('production_settings') or {}):
            settings[field] = actual['production_settings'][field]
        else:
            settings.pop(field, None)
    actual_masters = {int(m['idx']): m for m in actual.get('masters') or []}
    for master in merged.get('masters') or []:
        actual_master = actual_masters.get(int(master['idx']))
        if actual_master is not None and 'duration' in actual_master:
            master['duration'] = actual_master['duration']
    return merged


def persist_effective_storyboard(database, job_id, actual, *, expected_local_fingerprint):
    from . import node_state_machine
    with node_state_machine.atomic(database):
        row = database.execute('SELECT storyboard FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('content job disappeared during local compose')
        live = object_json(row['storyboard'])
        if edit_plan_fingerprint(live) != expected_local_fingerprint:
            raise ValueError('content local edit changed during compose preparation')
        merged = effective_storyboard(live, actual)
        database.execute('UPDATE jobs SET storyboard=? WHERE job_id=?',
            (json.dumps(merged, ensure_ascii=False, allow_nan=False), job_id))
    return merged


def probe_duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], capture_output=True,
        text=True, check=True, timeout=30)
    duration = float(result.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('content asset duration is invalid')
    return duration


def selected(database, assets_dir, job_id, master_idx, source='candidate'):
    if source not in {'candidate', 'active', 'prior'}:
        raise ValueError('content source must be candidate, active or prior')
    job = database.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    shot = database.execute('SELECT * FROM shots WHERE job_id=? AND idx=?', (job_id, int(master_idx))).fetchone()
    if job is None or shot is None:
        raise ValueError('content job/master not found')
    sb = object_json(job['storyboard']); qa = object_json(shot['video_qa'])
    master = next((m for m in sb.get('masters') or [] if int(m['idx']) == int(master_idx)), None)
    if master is None:
        raise ValueError('content master contract not found')
    if source == 'candidate':
        path = controlled_candidate_path(assets_dir, job_id, qa.get('candidate_path'))
    else:
        # Prior is the active old source while a replacement is awaiting review.
        operation = qa.get('review_operation') or {}
        raw = operation.get('prior_backup_path') if source == 'prior' and operation.get('phase') == 'committed' else shot['video_path']
        path = controlled_job_asset_path(assets_dir, job_id, raw, require_file=True)
    if path is None:
        raise ValueError('content selected source is unavailable or uncontrolled')
    return job, shot, sb, master, qa, path


def matching_human(qa, reqs, sha):
    req_hash = visual_contract.requirement_set_hash(reqs)
    records = qa.get('content_reviews')
    if not isinstance(records, list):
        return None
    return next((r for r in reversed(records) if isinstance(r, dict) and
        r.get('authority') == AUTHORITY and r.get('provenance') == 'human' and
        r.get('asset_sha256') == sha and r.get('requirement_set_hash') == req_hash), None)


def automatic(qa):
    raw = qa.get('result') or qa.get('diagnostic') or {}
    return raw.get('visual_evidence') if isinstance(raw, dict) else None


def proof(reqs, qa, sha, duration):
    human = matching_human(qa, reqs, sha)
    validator = visual_evidence.evaluate_human if human else visual_evidence.evaluate
    return validator(reqs, human or automatic(qa), asset_sha256=sha, duration_sec=duration)


def require_selected(database, assets_dir, job_id, master_idx, source):
    row = database.execute('SELECT storyboard FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    if row and not enabled(object_json(row['storyboard'])):
        return
    _, _, sb, master, qa, path = selected(database, assets_dir, job_id, master_idx, source)
    validate_contract(sb)
    reqs = requirements(master)
    if not reqs:
        return
    sha = visual_evidence.asset_fingerprint(path)
    try:
        report = proof(reqs, qa, sha, probe_duration(path))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError('content source proof unavailable; review visual content') from exc
    if report.get('passed') is not True:
        raise ValueError('content proof required; review visual content: ' + ', '.join(report['reasons']))


def validate_items(reqs, items, duration):
    ids = {r['id'] for r in reqs}
    if not isinstance(items, list) or len(items) != len(ids):
        raise ValueError('content review must explicitly cover each requirement')
    result = []; seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get('id') not in ids or item['id'] in seen:
            raise ValueError('content review has unknown/duplicate requirement IDs')
        seen.add(item['id']); status = item.get('status')
        if status not in STATUSES:
            raise ValueError('content review status must be explicit')
        description = str(item.get('observed_description') or '').strip()
        reason = str(item.get('reason') or '').strip()
        start, end = item.get('start_sec'), item.get('end_sec')
        if status == 'not_applicable':
            if not reason:
                raise ValueError('content waiver requires reason')
        if status == 'satisfied' or start is not None or end is not None:
            if (isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, (float, int))
                or not isinstance(end, (float, int)) or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end <= duration):
                raise ValueError('content observed interval must be finite and within actual duration')
        if status == 'satisfied' and not description:
            raise ValueError('content satisfied requires observed description')
        result.append({'id': item['id'], 'status': status, 'start_sec': start, 'end_sec': end,
                       'observed_description': description, 'reason': reason})
    return result


def retention(storyboard, rows, durations):
    validate_contract(storyboard)
    masters = {}; human_ids = []
    plans = {int(m['idx']): m for m in storyboard.get('masters') or []}
    for row in rows:
        idx = int(row['idx']); sha = visual_evidence.asset_fingerprint(Path(row['video_path']))
        qa = object_json(row['video_qa']); reqs = requirements(plans.get(idx, {}))
        human = matching_human(qa, reqs, sha)
        if human:
            human_ids.append(idx)
        masters[idx] = {'asset_sha256': sha, 'duration_sec': durations[idx],
                        'evidence': human or automatic(qa)}
    return visual_evidence.retention_report(storyboard, masters, verified_human_master_ids=human_ids)


def source_hashes(storyboard, rows, bgm_path):
    used = {int(e[key]) for e in storyboard.get('shots') or [] for key in ('video_master_idx', 'audio_master_idx') if key in e}
    paths = {int(r['idx']): Path(r['video_path']) for r in rows}
    if used - paths.keys():
        raise ValueError('content source missing')
    hashes = {str(idx): visual_evidence.asset_fingerprint(paths[idx]) for idx in sorted(used)}
    hashes['bgm'] = visual_evidence.asset_fingerprint(Path(bgm_path))
    return hashes


def require_final(database, assets_dir, job_id):
    job = database.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    if not job:
        raise ValueError('content job not found')
    sb = object_json(job['storyboard'])
    if not enabled(sb):
        return
    final = controlled_job_asset_path(assets_dir, job_id, job['final_path'], require_file=True)
    if final is None:
        raise ValueError('content final unavailable')
    try:
        report = object_json((final.parent/'content_retention.json').read_text(encoding='utf-8'))
        rows = database.execute("SELECT idx,video_path,video_qa FROM shots WHERE job_id=? AND state='OK'", (job_id,)).fetchall()
        bgm = controlled_job_asset_path(assets_dir, job_id, job['bgm_path'] or str(final.parent/'bgm.mp3'), require_file=True)
        if bgm is None:
            raise ValueError('content BGM unavailable')
        if (report.get('passed') is not True or report.get('final_sha256') != visual_evidence.asset_fingerprint(final)
            or report.get('contract_hash') != contract_hash(sb)
            or report.get('edit_plan_fingerprint') != edit_plan_fingerprint(sb)
            or report.get('source_hashes') != source_hashes(sb, rows, bgm)):
            raise ValueError('content final report missing or stale; local recompose required')
        current = retention(sb, rows, {int(r['idx']): probe_duration(Path(r['video_path'])) for r in rows})
        if current.get('passed') is not True or any(current.get(key) != report.get(key) for key in
            ('master_hashes', 'items', 'kept_intervals', 'visible_intervals')):
            raise ValueError('content retention proof stale; review or local recut required')
        return report
    except (OSError, subprocess.SubprocessError, TypeError, KeyError) as exc:
        raise ValueError('content final report unavailable; local recompose required') from exc
