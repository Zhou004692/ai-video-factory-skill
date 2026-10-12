"""Read-only repair choices and server-authorized local picture overlays.

An observed action can be recovered from accepted footage; an authored action,
waiver or missing event cannot. No function here submits provider work.
"""
from copy import deepcopy
import math
from pathlib import Path

from . import content_review as cr, master_transcriptions, provider_tasks, visual_evidence
from .clients import seedance

OVERLAY_KEY = 'authorized_local_visual_edit'
ALIGNMENT_KEY = 'authorized_cached_visual_alignment'
AUTHORITY = 'management_local_visual_edit_v1'
TERMINATED = {'PUBLISHED', 'REJECTED', 'REJECTED_PENDING_RECONCILIATION'}
AUDIO_FIELDS = ('audio_master_idx', 'audio_start', 'duration', 'dialogue', 'voiceover')


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('content local interval must be finite')
    return float(value)


def safe_picture(edit):
    """Require explicit authored coverage; absence of lip metadata is not consent."""
    if edit.get('requires_lip_sync') or edit.get('lip_sync_required') or edit.get('speech_mode') in {'on_screen', 'onscreen'} or edit.get('coverage') == 'visible_speaking_mouth':
        return False
    return (edit.get('speech_mode') in {'off_screen', 'offscreen'} or
            edit.get('coverage') in {'reaction', 'faceless', 'broll', 'b_roll', 'B-roll'} or
            edit.get('shot_type') in {'reaction', 'faceless', 'broll', 'b_roll'})


def pending_candidate(qa):
    operation = qa.get('review_operation') or {}
    # Accepted-but-not-yet-cleaned audit references do not constitute a pending
    # candidate. The separate candidate decision already owns that gate.
    return bool(qa.get('candidate_path') and not (operation.get('phase') == 'committed'
        and operation.get('decision') in {'accept_candidate', 'keep_prior'}))


def authored_safe(database, job_id, edits, shots):
    from . import production_compiler
    job = database.execute('SELECT production_spec_json FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    spec = cr.object_json(job['production_spec_json'])
    frozen = production_compiler.ensure_director_timeline(spec.get('runtime_storyboard') or {})
    authored = {e['idx']: e for e in frozen.get('shots') or []}
    current = {e['idx']: e for e in shots}
    permission_fields = ('speech_mode', 'coverage', 'shot_type', 'requires_lip_sync', 'lip_sync_required')
    return all(change['edit_idx'] in authored and safe_picture(authored[change['edit_idx']]) and
        all(authored[change['edit_idx']].get(key) == current[change['edit_idx']].get(key) for key in permission_fields)
        for change in edits)


def apply_edits(sb, edits, durations):
    if not isinstance(edits, list) or not edits:
        raise ValueError('content explicit local picture edits required')
    result = deepcopy(sb); by_idx = {e['idx']: e for e in result.get('shots') or []}; seen = set()
    for change in edits:
        if not isinstance(change, dict) or set(change) != {'edit_idx', 'video_start'}:
            raise ValueError('content local recut only accepts edit_idx/video_start')
        idx = change['edit_idx']
        if isinstance(idx, bool) or not isinstance(idx, int) or idx not in by_idx or idx in seen:
            raise ValueError('content unknown or duplicate local edit')
        seen.add(idx); edit = by_idx[idx]
        if not safe_picture(edit):
            raise ValueError('content on-screen mouth/same-source speech constraint; picture movement requires safe authored coverage')
        start = number(change['video_start']); duration = number(edit.get('duration'))
        actual = durations.get(int(edit.get('video_master_idx', -1)))
        if actual is None or not 0 <= start < start + duration <= actual:
            raise ValueError('content local picture source window exceeds actual asset; no clamp')
        edit['video_start'] = start
    return result


def source_bindings(sb, rows, durations):
    bindings = {}
    for row in rows:
        idx = int(row['idx'])
        bindings[str(idx)] = {'asset_sha256': visual_evidence.asset_fingerprint(row['video_path']),
                            'duration_sec': durations[idx], 'asset_path': str(Path(row['video_path']).resolve())}
    return bindings


def edit_fingerprint(sb):
    try:
        return cr.edit_plan_fingerprint(sb)
    except ValueError:
        # Nonfinite/invalid inputs are displayed for manual review, never
        # normalized into an executable command or a false source observation.
        return None


def cached_alignment(database, job_id, sb, rows):
    """Plan existing ASR timing changes without calling ASR or writing state."""
    from .steps import s6_compose
    from . import subtitles
    result = deepcopy(sb); paths = {int(r['idx']): r['video_path'] for r in rows}
    for e in sb.get('shots') or []:
        number(e.get('audio_start', 0)); number(e.get('video_start', 0)); number(e['duration'])
    types = {int(m['idx']): m.get('type') for m in sb.get('masters') or []}
    pending = {int(e.get('audio_master_idx', e.get('video_master_idx', -1))) for e in sb.get('shots') or []
        if s6_compose._speech_timeline_needs_refresh(e) and
        (e.get('subtitle') or e.get('subtitle_cues') or types.get(int(e.get('audio_master_idx', e.get('video_master_idx', -1)))) == 'dialogue')}
    if not (sb.get('production_settings') or {}).get('asr_enabled', True):
        pending = set()
    bindings = {}; missing = []
    for idx in sorted(pending):
        if idx not in paths:
            missing.append(idx); continue
        transcription = master_transcriptions.load_matching(database, job_id, idx, paths[idx])
        if transcription is None:
            missing.append(idx); continue
        bindings[str(idx)] = cr.digest(master_transcriptions._payload(transcription))
        edits = [e for e in result.get('shots') or [] if int(e.get('audio_master_idx', e.get('video_master_idx', -1))) == idx]
        terminal = max(edits, key=lambda e: number(e.get('audio_start', 0)) + number(e['duration']))
        video_idx = int(terminal.get('video_master_idx', idx))
        available = min(cr.probe_duration(paths[idx]), number(terminal.get('audio_start', 0)) +
                        max(0., cr.probe_duration(paths[video_idx]) - number(terminal.get('video_start', 0))))
        s6_compose._align_internal_edits_to_speech_pauses(result, idx, transcription)
        s6_compose._extend_terminal_edit_for_speech(result, idx, transcription, available_duration=available)
        subtitles.apply_master_transcription(result, idx, transcription)
    before = {e['idx']: e for e in sb.get('shots') or []}
    timing_changes = any(any(e.get(f) != before[e['idx']].get(f) for f in AUDIO_FIELDS) for e in result.get('shots') or [])
    return {'available': not missing, 'missing_master_ids': missing, 'cache_bindings': bindings,
            'audio_timing_changes': timing_changes}


def local_proposal(sb, rows, durations, master_idx, proof):
    """Search bounded complete output runs without changing audio or shot order."""
    if not proof.get('passed') or not any(i['status'] == 'satisfied' for i in proof['items']):
        return None, 'missing_observed_action'
    if cr.retention(sb, rows, durations)['passed']:
        return None, 'current_action_already_retained'
    shots = sb.get('shots') or []
    try:
        fades = visual_evidence.picture_transitions(shots)
        # Ensure an invalid current timeline is never silently normalized.
        for edit in shots:
            start = number(edit.get('video_start', 0)); length = number(edit.get('duration'))
            actual = durations.get(int(edit.get('video_master_idx', -1)))
            if actual is None or not 0 <= start < start + length <= actual:
                return None, 'invalid_or_unknown_timeline'
            audio_start = number(edit.get('audio_start', 0))
            audio_idx = int(edit.get('audio_master_idx', edit.get('video_master_idx', -1)))
            if audio_idx not in durations or not 0 <= audio_start < audio_start + length <= durations[audio_idx]:
                return None, 'invalid_or_unknown_timeline'
            if not safe_picture(edit) and audio_idx == int(edit.get('video_master_idx', -1)) and abs(start - audio_start) > .001:
                return None, 'on_screen_or_unknown_coverage'
        observations = [i for i in proof['items'] if i['status'] == 'satisfied']
        starts = []
        for index, shot in enumerate(shots):
            if int(shot.get('video_master_idx', -1)) != master_idx or not safe_picture(shot):
                continue
            # Enumerate adjacent clear output segments: continuity requires both
            # source adjacency and no interior fade, exactly as retention does.
            run = []
            for end_index in range(index, len(shots)):
                candidate = shots[end_index]
                if int(candidate.get('video_master_idx', -1)) != master_idx or not safe_picture(candidate):
                    break
                if run and (fades[end_index]['video_fade_in_sec'] or fades[end_index - 1]['video_fade_out_sec']):
                    break
                run.append(candidate)
                length = sum(number(e['duration']) for e in run)
                left = fades[index]['video_fade_in_sec']; right = fades[end_index]['video_fade_out_sec']
                first = min(number(i['start_sec']) for i in observations)
                last = max(number(i['end_sec']) for i in observations)
                lower = max(0., last + right - length); upper = min(first - left, durations[master_idx] - length)
                if lower > upper:
                    continue
                start = min(max(number(shot.get('video_start', 0)), lower), upper)
                edits = []; cursor = start
                for e in run:
                    edits.append({'edit_idx': e['idx'], 'video_start': cursor}); cursor += number(e['duration'])
                changed = [e for e in edits if abs(e['video_start'] - number(next(s for s in shots if s['idx'] == e['edit_idx']).get('video_start', 0))) > .000001]
                if not changed:
                    continue
                proposed = apply_edits(sb, changed, durations)
                report = cr.retention(proposed, rows, durations)
                if report['passed']:
                    starts.append((len(changed), sum(abs(e['video_start'] - number(next(s for s in shots if s['idx'] == e['edit_idx']).get('video_start', 0))) for e in changed), changed, report))
        if starts:
            _, _, edits, report = min(starts, key=lambda value: value[:2])
            return {'edits': edits, 'retention': report}, 'complete_observed_action_in_safe_source_window'
    except (ValueError, TypeError, KeyError, visual_evidence.ContentReviewRequired):
        return None, 'invalid_or_unknown_timeline'
    if not any(int(e.get('video_master_idx', -1)) == master_idx and safe_picture(e) for e in shots):
        return None, 'on_screen_or_unknown_coverage'
    unfaded = deepcopy(sb)
    for edit in unfaded.get('shots') or []:
        edit['transition_type'] = 'none'; edit['transition_duration_sec'] = 0
    if cr.retention(unfaded, rows, durations)['passed']:
        return None, 'local_transition_adjustment_required'
    if any(f['video_fade_in_sec'] or f['video_fade_out_sec'] for f in fades):
        clear_plan, _reason = local_proposal(unfaded, rows, durations, master_idx, proof)
        if clear_plan is not None:
            return None, 'local_transition_adjustment_required'
    # Failure to find a combined local arrangement is not proof that an event
    # is absent, or that paid generation is necessary. Distinguish a genuinely
    # short clear output window from this bounded planner's search boundary.
    capacities = []; run_length = 0.
    for index, edit in enumerate(shots):
        if int(edit.get('video_master_idx', -1)) != master_idx or not safe_picture(edit):
            run_length = 0.; continue
        if index and (fades[index]['video_fade_in_sec'] or fades[index - 1]['video_fade_out_sec']):
            run_length = 0.
        run_length += number(edit['duration'])
        capacities.append(run_length - fades[index]['video_fade_out_sec'])
    if any(number(item['end_sec']) - number(item['start_sec']) > max(capacities or [0.]) for item in observations):
        return None, 'source_window_insufficient_for_complete_action'
    return None, 'local_picture_plan_requires_review'


def plan(database, assets_dir, job_id, master_idx, source='candidate'):
    job, shot, sb, master, qa, path = cr.selected(database, assets_dir, job_id, master_idx, source)
    cr.validate_contract(sb)
    reqs = cr.requirements(master); sha = visual_evidence.asset_fingerprint(path); duration = cr.probe_duration(path)
    proof = cr.proof(reqs, qa, sha, duration)
    rows = database.execute('SELECT idx,state,video_path,video_qa FROM shots WHERE job_id=? ORDER BY idx', (job_id,)).fetchall()
    pending = provider_tasks.unresolved_task_ids(database, job_id)
    candidate_pending = any(pending_candidate(cr.object_json(r['video_qa'])) for r in rows)
    active_ok = source == 'active' and all(r['state'] == 'OK' for r in rows) and not candidate_pending
    durations = {}; bindings = {}; retention = None; proposal = None
    reason = 'accept_current_candidate_first' if source != 'active' or candidate_pending else 'all_masters_must_be_accepted'
    if active_ok:
        for row in rows:
            active = cr.selected(database, assets_dir, job_id, row['idx'], 'active')[-1]
            durations[int(row['idx'])] = cr.probe_duration(active)
        bindings = source_bindings(sb, rows, durations)
        retention = cr.retention(sb, rows, durations)
        proposal, reason = local_proposal(sb, rows, durations, int(master_idx), proof)
    if pending:
        proposal = None; reason = 'reconcile_existing_paid_task_before_any_new_purchase'
    if job['state'] in TERMINATED:
        proposal = None; reason = 'terminated_job'
    if not proof['passed']:
        proposal = None
        invalid = {'content_stale_evidence', 'content_unverified_provenance', 'content_invalid_evidence'}
        reason = ('observed_content_conflict' if 'content_conflicting' in proof['reasons'] and not invalid.intersection(proof['reasons'])
                  else 'missing_or_stale_evidence_review_first')
    if pending:
        reason = 'reconcile_existing_paid_task_before_any_new_purchase'
    if job['state'] in TERMINATED:
        reason = 'terminated_job'
    graph = job['execution_engine'] == 'graph_v2'
    node = database.execute("SELECT state,error_code FROM production_nodes WHERE job_id=? AND node_id='local_compose'", (job_id,)).fetchone()
    if proposal and (not graph or not node or node['state'] not in {'SUCCEEDED', 'REVIEW_REQUIRED'}):
        proposal = None; reason = 'local_compose_in_progress' if graph and node else 'local_graph_recovery_required'
    if proposal and node['state'] == 'REVIEW_REQUIRED' and node['error_code'] != 'content_review_required':
        proposal = None; reason = 'local_graph_recovery_required'
    if proposal and not authored_safe(database, job_id, proposal['edits'], sb['shots']):
        proposal = None; reason = 'on_screen_or_unknown_coverage'
    alignment = {'available': False, 'missing_master_ids': [], 'cache_bindings': {}, 'audio_timing_changes': False}
    recovery = {'executable_now': False}
    if active_ok and not pending and job['state'] not in TERMINATED:
        try:
            alignment = cached_alignment(database, job_id, sb, rows)
            from . import production_compiler
            spec = cr.object_json(job['production_spec_json'])
            frozen = production_compiler.ensure_director_timeline(spec.get('runtime_storyboard') or {})
            frozen_alignment = cached_alignment(database, job_id, frozen, rows)
            recovery = {'executable_now': graph and node is not None and node['state'] in {'SUCCEEDED', 'REVIEW_REQUIRED'} and
                         (node['state'] == 'SUCCEEDED' or node['error_code'] == 'content_review_required') and
                         frozen_alignment['available'] and (OVERLAY_KEY in sb or alignment['audio_timing_changes']),
                        'discards_prior_overlay': OVERLAY_KEY in sb,
                        'cache_bindings': frozen_alignment['cache_bindings'],
                        'preserved_scope': ['source_assets', 'dialogue', 'BGM', 'anchors', 'accepted_masters', 'cost_ledger'],
                        'changes_scope': ['abandon_authorized_picture_overlay', 'align_local_audio_picture_boundaries_to_cached_speech'],
                        'approval_requirement': 'explicit_discard_and_cached_alignment_confirmation'}
            if proposal and (not alignment['available'] or alignment['audio_timing_changes']):
                proposal = None; reason = 'speech_alignment_required_before_recut'
        except (ValueError, KeyError, TypeError):
            if proposal:
                proposal = None; reason = 'invalid_or_unknown_timeline'
    diagnostic = qa.get('result') or qa.get('diagnostic') or {}
    automatic_evidence = cr.automatic(qa)
    diagnostics_bound = isinstance(automatic_evidence, dict) and automatic_evidence.get('asset_sha256') == sha
    diagnostic_reasons = qa.get('reasons') or []
    if isinstance(diagnostic_reasons, str):
        diagnostic_reasons = [diagnostic_reasons]
    if not isinstance(diagnostic_reasons, list):
        diagnostic_reasons = []
    diagnostic_reasons = [item for item in diagnostic_reasons if isinstance(item, str)]
    mouth_defect = 'lip_sync_mismatch' in diagnostic_reasons or isinstance(diagnostic, dict) and diagnostic.get('lip_sync') is False
    if mouth_defect and proof['passed'] and not pending and job['state'] not in TERMINATED:
        proposal = None; reason = 'mouth_defect_requires_repair_or_review'
    settings = sb.get('production_settings') or {}; snapshot = settings.get('generation_snapshot') or {}
    model = snapshot.get('model') or seedance.DEFAULT_MODEL; profile = seedance.capability_profile(model)
    guards = {'expected_asset_sha256': sha, 'expected_requirement_set_hash': cr.visual_contract.requirement_set_hash(reqs),
              'expected_edit_plan_fingerprint': edit_fingerprint(sb), 'expected_spec_hash': job['production_spec_hash'] or '',
              'expected_contract_hash': cr.contract_hash(sb), 'expected_source_bindings': bindings,
              'expected_asr_cache_bindings': {'current': alignment['cache_bindings'], 'frozen': recovery.get('cache_bindings', {})}}
    scope = {'master_idx': int(master_idx), 'requirement_ids': [r['id'] for r in reqs]}
    preserved = ['dialogue', 'audio_source', 'audio_start', 'duration', 'BGM', 'anchors', 'accepted_masters', 'task_ids', 'cost_ledger']
    def route(kind, why, *, available=False, cost=None, basis='unknown', approval='independent_paid_preparation_and_budget_confirmation'):
        return {'id': kind, 'reason': why, 'affected_scope': scope,
                'preserved_scope': preserved if kind in {'local_recut', 'manual_review'} else
                    ['other_accepted_masters', 'original_selected_source_until_candidate_acceptance', 'BGM', 'anchors', 'prior_task_ids', 'historical_cost_ledger'],
                'cost': {'known': cost is not None, 'estimated_cost_cents': cost, 'basis': basis},
                'approval_requirement': approval, 'executable_now': available,
                'execution_boundary': 'local_compose_only' if kind == 'local_recut' else 'read_only_recommendation_no_provider_submit'}
    local = route('local_recut', reason, available=proposal is not None, cost=0, basis='no_new_provider_calls', approval='explicit_picture_window_confirmation')
    if proposal:
        proposed = apply_edits(sb, proposal['edits'], durations)
        local['plan'] = {**proposal, 'windows': [{'edit_idx': e['idx'],
            'current': [e.get('video_start', 0), e.get('video_start', 0) + e['duration']],
            'proposed': [next(n for n in proposed['shots'] if n['idx'] == e['idx'])['video_start'],
                         next(n for n in proposed['shots'] if n['idx'] == e['idx'])['video_start'] + e['duration']],
            'audio_master_idx': e.get('audio_master_idx', e['video_master_idx']), 'audio_start': e.get('audio_start', 0),
            'duration': e['duration']} for e in sb['shots'] if any(c['edit_idx'] == e['idx'] for c in proposal['edits'])]}
    native = route('bounded_native_edit', 'capability_available_execution_requires_independent_repair_contract' if 'edit' in profile['task_types'] else 'model_does_not_support_edit')
    native['capability_supported'] = 'edit' in profile['task_types']; native['implemented'] = False
    native['preparation_options'] = ['existing_lip_sync_preparation_then_budget_confirmation'] if mouth_defect else ['new_bounded_visual_edit_contract_required']
    native['reference_duration_supported'] = duration <= profile['reference_duration_limits'].get('video', duration)
    extend = route('extend', 'prepare_extension_and_budget_confirmation' + ('_graph_repair_contract_required' if graph else '') if 'extend' in profile['task_types'] else 'model_does_not_support_extend')
    extend['capability_supported'] = 'extend' in profile['task_types']; extend['preparation_only'] = True
    # Only the existing duration/resolution estimate is known. Edit/extend output
    # length is not yet specified; never report zero or a cheaper comparison.
    regenerate_cost = None
    try:
        regenerate_cost = seedance.estimate_cost_cents(seedance.normalize_generation_duration(duration), snapshot.get('resolution') or '720p')
    except ValueError:
        pass
    regenerate = route('regenerate', 'prepare_target_master_and_budget_confirmation' + ('_graph_repair_contract_required' if graph else ''), cost=regenerate_cost, basis='existing_seedance_duration_resolution_estimate_not_billed_price')
    regenerate['preparation_only'] = True
    manual = route('manual_review', 'reconcile_provider_GET_first' if pending else reason, approval='explicit_observation_or_reasoned_waiver')
    return {'job_id': job_id, 'master_idx': int(master_idx), 'source': source, 'asset_sha256': sha,
            'duration_sec': duration, 'requirement_set_hash': guards['expected_requirement_set_hash'],
            'requirements': reqs, 'evidence': proof, 'retention': retention, 'guards': guards,
            'defects': [{'code': code, 'provenance': 'selected_content_proof', 'bound_to_selected': True} for code in proof['reasons']] +
                [{'code': code, 'provenance': 'automatic_diagnostic', 'bound_to_selected': diagnostics_bound} for code in diagnostic_reasons],
            'waivers': [i for i in proof['items'] if i['status'] == 'not_applicable'],
            'cached_alignment': alignment, 'local_alignment_recovery': recovery,
            'unresolved_provider_task_ids': pending, 'recommended_route': 'local_recut' if proposal else
                ('local_recut' if reason == 'local_transition_adjustment_required' else
                 'bounded_native_edit' if reason in {'observed_content_conflict', 'mouth_defect_requires_repair_or_review'} and native['capability_supported'] and (reason == 'observed_content_conflict' or diagnostics_bound) else
                 'extend' if reason == 'source_window_insufficient_for_complete_action' and extend['capability_supported'] else 'manual_review'),
            'model_capabilities': {'model': model, 'task_types': sorted(profile['task_types']),
                                   'reference_duration_limits': profile['reference_duration_limits']},
            'routes': [local, native, extend, regenerate, manual]}


def make_overlay(sb, edits, guards, durations):
    edited = apply_edits(sb, edits, durations)
    overlay = {'version': 1, 'authority': AUTHORITY, 'spec_hash': guards['expected_spec_hash'],
        'contract_hash': guards['expected_contract_hash'], 'source_bindings': guards['expected_source_bindings'],
        'prior_edit_plan_fingerprint': guards['expected_edit_plan_fingerprint'],
        'asr_cache_bindings': guards['expected_asr_cache_bindings']['current'],
        'base_shots': deepcopy(sb['shots']), 'edits': deepcopy(edits),
        'effective_shots': deepcopy(edited['shots']), 'effective_edit_plan_fingerprint': cr.edit_plan_fingerprint(edited)}
    edited[OVERLAY_KEY] = overlay
    edited.pop(ALIGNMENT_KEY, None)
    return edited


def verify_asr_bindings(database, job_id, rows, bindings):
    paths = {str(r['idx']): r['video_path'] for r in rows}
    for idx, expected in bindings.items():
        transcription = master_transcriptions.load_matching(database, job_id, int(idx), paths[idx])
        if transcription is None or cr.digest(master_transcriptions._payload(transcription)) != expected:
            raise ValueError('content authorized ASR cache changed; new ASR requests are not authorized')


def verify_alignment(database, job_id, sb, rows):
    marker = sb.get(ALIGNMENT_KEY)
    if marker is None:
        return
    if not rows or any(dict(r).get('state', 'OK') != 'OK' or pending_candidate(cr.object_json(r['video_qa'])) for r in rows):
        raise ValueError('content cached alignment requires accepted masters without pending candidates')
    job = database.execute('SELECT production_spec_hash FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    if marker.get('authority') != AUTHORITY or marker.get('spec_hash') != job['production_spec_hash'] or marker.get('contract_hash') != cr.contract_hash(sb):
        raise ValueError('content authorized cached alignment is stale')
    bindings = marker['source_bindings']
    if {str(r['idx']): str(Path(r['video_path']).resolve()) for r in rows} != {idx: b['asset_path'] for idx,b in bindings.items()}:
        raise ValueError('content authorized cached alignment source path changed')
    durations = {int(r['idx']): cr.probe_duration(r['video_path']) for r in rows}
    if source_bindings(sb, rows, durations) != bindings:
        raise ValueError('content authorized cached alignment source changed')
    verify_asr_bindings(database, job_id, rows, marker['asr_cache_bindings'])
    # Include readiness, so a newly required/unbound speech master cannot
    # silently fall back to a network ASR request inside a free operation.
    readiness = cached_alignment(database, job_id, sb, rows)
    if not readiness['available'] or any(idx not in marker['asr_cache_bindings'] for idx in readiness['cache_bindings']):
        raise ValueError('content cached alignment needs unapproved ASR')


def verify_overlay(database, job_id, sb, rows, *, spec_hash=None):
    overlay = sb.get(OVERLAY_KEY)
    if overlay is None:
        return None
    job = database.execute('SELECT storyboard,production_spec_hash FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    live = cr.object_json(job['storyboard'])
    if not rows or any(dict(r).get('state', 'OK') != 'OK' or pending_candidate(cr.object_json(r['video_qa'])) for r in rows):
        raise ValueError('content local overlay requires all accepted masters and no pending candidate')
    if ({str(r['idx']): str(Path(r['video_path']).resolve()) for r in rows} !=
        {idx: binding.get('asset_path') for idx, binding in (overlay.get('source_bindings') or {}).items()}):
        raise ValueError('content authorized local overlay source path changed')
    verify_asr_bindings(database, job_id, rows, overlay.get('asr_cache_bindings') or {})
    readiness = cached_alignment(database, job_id, sb, rows)
    if not readiness['available'] or any(idx not in overlay.get('asr_cache_bindings', {}) for idx in readiness['cache_bindings']):
        raise ValueError('content local recut cannot request new or unapproved ASR')
    durations = {int(r['idx']): cr.probe_duration(r['video_path']) for r in rows}
    if (overlay.get('authority') != AUTHORITY or overlay.get('version') != 1 or
        overlay.get('spec_hash') != (spec_hash if spec_hash is not None else job['production_spec_hash'] or '') or
        overlay.get('contract_hash') != cr.contract_hash(live) or
        overlay.get('contract_hash') != cr.contract_hash(sb) or
        overlay.get('source_bindings') != source_bindings(live, rows, durations) or
        overlay.get('effective_edit_plan_fingerprint') != cr.edit_plan_fingerprint(live)):
        raise ValueError('content authorized local overlay is stale; review current source/spec/requirements/edit')
    base = deepcopy(live); base['shots'] = overlay['base_shots']
    if cr.edit_plan_fingerprint(base) != overlay.get('prior_edit_plan_fingerprint'):
        raise ValueError('content local overlay prior approved plan fingerprint changed')
    if not authored_safe(database, job_id, overlay['edits'], base['shots']):
        raise ValueError('content local overlay no longer matches authored safe coverage')
    proposed = apply_edits(base, overlay['edits'], durations)
    # ASR may add subtitle cues, but may never move protected audio clocks.
    if len(proposed['shots']) != len(overlay['effective_shots']):
        raise ValueError('content local overlay edit count changed')
    for before, after in zip(proposed['shots'], overlay['effective_shots']):
        for field in ('idx', 'video_master_idx', 'video_start', *AUDIO_FIELDS):
            if before.get(field) != after.get(field):
                raise ValueError('content local overlay changed protected picture/audio fields')
    effective = deepcopy(live); effective['shots'] = deepcopy(overlay['effective_shots'])
    if cr.retention(effective, rows, durations)['passed'] is not True:
        raise ValueError('content authorized local overlay retention is stale')
    return overlay


def adapter_storyboard(database, job_id, frozen, spec_hash):
    row = database.execute('SELECT storyboard FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    live = cr.object_json(row['storyboard'])
    if OVERLAY_KEY not in live and ALIGNMENT_KEY not in live:
        return frozen
    rows = database.execute("SELECT idx,state,video_path,video_qa FROM shots WHERE job_id=? ORDER BY idx", (job_id,)).fetchall()
    try:
        if not rows or any(r['state'] != 'OK' or pending_candidate(cr.object_json(r['video_qa'])) for r in rows):
            raise ValueError('content local overlay requires accepted masters without candidates')
        if ALIGNMENT_KEY in live and OVERLAY_KEY not in live:
            result = deepcopy(frozen); result[ALIGNMENT_KEY] = deepcopy(live[ALIGNMENT_KEY])
            verify_alignment(database, job_id, result, rows)
            return result
        verify_overlay(database, job_id, live, rows, spec_hash=spec_hash)
        if cr.contract_hash(frozen) != cr.contract_hash(live):
            raise ValueError('content local overlay frozen requirements changed')
        result = deepcopy(frozen)
        result['shots'] = deepcopy(live['shots'])
        result['hook'] = live.get('hook')
        for key in cr.LOCAL_SETTINGS:
            if key in (live.get('production_settings') or {}):
                result.setdefault('production_settings', {})[key] = live['production_settings'][key]
        result[OVERLAY_KEY] = deepcopy(live[OVERLAY_KEY])
        return result
    except (ValueError, OSError, KeyError) as exc:
        raise visual_evidence.ContentReviewRequired({'passed': False, 'reasons': ['content_stale_local_overlay']}) from exc


def restore_after_asr(sb, approved_shots):
    overlay = sb.get(OVERLAY_KEY)
    if not overlay:
        return False
    before = {e['idx']: e for e in approved_shots}; current = {e['idx']: e for e in sb['shots']}
    if before.keys() != current.keys():
        raise visual_evidence.ContentReviewRequired({'passed': False, 'reasons': ['content_local_audio_timing_changed']})
    for idx, e in current.items():
        if any(e.get(f) != before[idx].get(f) for f in AUDIO_FIELDS):
            raise visual_evidence.ContentReviewRequired({'passed': False, 'reasons': ['content_local_audio_timing_changed']})
    changed = False
    # Restore the full approved picture baseline too: native same-master ASR
    # synchronization otherwise shifts unrelated, unselected B-roll windows.
    # The only difference from the prior plan remains the explicit edits.
    for idx, edit in current.items():
        wanted = before[idx].get('video_start', 0)
        if not safe_picture(edit):
            # A protected on-screen/native mouth clock must either remain
            # aligned or stop for review. It cannot inherit a silent-picture
            # exemption from an adjacent, authorized off-screen recut.
            if edit.get('video_start', 0) != wanted:
                raise visual_evidence.ContentReviewRequired({'passed': False, 'reasons': ['content_local_mouth_clock_changed']})
            continue
        changed = changed or edit.get('video_start', 0) != wanted
        if 'video_start' in before[idx]:
            edit['video_start'] = wanted
        else:
            edit.pop('video_start', None)
    overlay['effective_shots'] = deepcopy(sb['shots'])
    overlay['effective_edit_plan_fingerprint'] = cr.edit_plan_fingerprint(sb)
    return changed
