"""Server-authorized, source-bound paid repair attempts on an existing graph.

Original Production Spec, nodes and provider ledger are never replaced. Events
freeze preparation and confirmation; one additive node owns one paid attempt.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
import secrets
import subprocess

from . import content_review, node_state_machine, provider_tasks, visual_contract, visual_evidence
from .clients import ffmpeg, seedance

PREPARED = 'native_visual_repair_prepared'
AUTHORIZED = 'native_visual_repair_authorized'
CLOSED = 'native_visual_repair_closed'
AUTHORITY = 'native_visual_repair_v1'


class StaleRepairError(ValueError):
    pre_submit = True
    non_retryable = True


def finite(value, name):
    try: result = float(value)
    except (TypeError, ValueError): raise ValueError(f'{name} must be finite') from None
    if not math.isfinite(result): raise ValueError(f'{name} must be finite')
    return result


def probe_source(path):
    """Picture duration is independent of container/audio duration."""
    result = subprocess.run([ffmpeg.executable('ffprobe'), '-v', 'error', '-show_streams',
        '-show_format', '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=30)
    info = json.loads(result.stdout)
    video = next((s for s in info['streams'] if s.get('codec_type') == 'video'), None)
    if not video: raise ValueError('selected source has no picture stream; choose another source')
    duration = finite(video.get('duration'), 'picture duration')
    numerator, denominator = str(video.get('avg_frame_rate') or '0/1').split('/')
    divisor=finite(denominator,'FPS denominator')
    if divisor<=0:raise ValueError('source FPS is unknown; choose another source')
    fps = finite(numerator, 'FPS') / divisor
    width, height = int(video['width']), int(video['height'])
    if not (4 <= duration <= 30 and 24 <= fps <= 60 and 300 <= width <= 6000
            and 300 <= height <= 6000 and .4 <= width / height <= 2.5
            and 407696 <= width * height <= 8295044 and path.stat().st_size <= 200 * 1024 * 1024):
        raise ValueError('selected source is unsupported for native edit: 4–30s, 24–60FPS, supported dimensions/size required; use manual review or another source')
    return {'picture_duration_sec':duration, 'fps':fps, 'width':width, 'height':height,
            'size_bytes':path.stat().st_size, 'has_audio':any(s.get('codec_type')=='audio' for s in info['streams'])}


def event(database, job_id, step, payload):
    database.execute('INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)',
        (job_id, step, 'info', json.dumps(payload, ensure_ascii=False, allow_nan=False)))


def current(database, job_id):
    rows = database.execute('SELECT step,message FROM events WHERE job_id=? AND step IN (?,?) ORDER BY id DESC',
        (job_id, PREPARED, CLOSED)).fetchall()
    closed = set()
    for row in rows:
        value = content_review.object_json(row['message'])
        if row['step'] == CLOSED: closed.add(value.get('attempt_id'))
        elif value.get('attempt_id') not in closed: return value
    return None


def snapshot(database, assets, job_id, idx, source):
    job, shot, sb, master, qa, path = content_review.selected(database, assets, job_id, idx, source)
    spec = content_review.object_json(job['production_spec_json'])
    if job['execution_engine'] != 'graph_v2' or job['production_spec_version'] != 2 or not spec.get('runtime_storyboard'):
        raise ValueError('native repair requires an existing frozen graph_v2; legacy regeneration remains a separate manual route')
    frozen = spec['runtime_storyboard']
    if spec.get('spec_hash') != job['production_spec_hash'] or content_review.contract_hash(frozen) != content_review.contract_hash(sb):
        raise StaleRepairError('native repair content/frozen spec is stale; refresh review')
    guards = {'source':source, 'source_path':str(path), 'asset_sha256':visual_evidence.asset_fingerprint(path),
        'spec_hash':job['production_spec_hash'], 'spec_digest':content_review.digest(spec),
        'contract_hash':content_review.contract_hash(sb), 'requirement_set_hash':visual_contract.requirement_set_hash(content_review.requirements(master)),
        'effective_edit_hash':content_review.edit_plan_fingerprint(sb), 'master_hash':content_review.digest(master),
        'selected_qa_hash':content_review.digest(qa), 'shot_state':shot['state'], 'active_path':str(shot['video_path'] or ''),
        'current_cost_cents':int(job['cost_cents'] or 0), 'media':probe_source(path)}
    return job, shot, sb, master, qa, path, guards


def prepare(service, job_id, idx, *, kind='edit', start_sec=None, end_sec=None,
            direction='', extension_sec=None, source='active', source_confirmed=False, affected_requirement_ids=None,
            continuation_only_confirmed=False):
    if source not in {'active','candidate'}: raise ValueError('explicit source must be active or candidate')
    if source=='candidate' and source_confirmed is not True:
        raise ValueError('candidate source requires explicit full-preview confirmation')
    direction = str(direction or '').strip()
    if not direction: raise ValueError('readable target direction is required')
    if 'seedance-2-5' not in seedance.DEFAULT_MODEL:
        raise ValueError('native edit price/support unknown for configured model; use manual/alternate route')
    with node_state_machine.atomic(service.database):
        job, shot, sb, master, qa, path, guards = snapshot(service.database, service.data_dir/'assets', job_id, idx, source)
        service._assert_no_unresolved_provider_tasks(job_id)
        prior = current(service.database, job_id)
        if job['state'] not in {'COMPOSED','FAILED','BLOCKED','PRODUCTION_REVIEW_REQUIRED','READY_TO_PUBLISH','STORYBOARDED'}:
            raise ValueError('repair is unavailable in current state; reconcile provider task or review current candidate')
        if service.database.execute("SELECT 1 FROM shots WHERE job_id=? AND idx!=? AND state!='OK'", (job_id,idx)).fetchone():
            raise ValueError('other masters need reconciliation/review before repair')
        if service.database.execute("SELECT 1 FROM production_nodes WHERE job_id=? AND state IN ('RUNNING','READY','ACTION_REQUIRED','BUDGET_REQUIRED')", (job_id,)).fetchone():
            raise ValueError('pending operation must be reconciled/reviewed before preparing another repair')
        duration = guards['media']['picture_duration_sec']
        if kind == 'edit':
            start, end = finite(start_sec,'start'), finite(end_sec,'end')
            if not 0 <= start < end <= duration: raise ValueError('edit interval is out of picture bounds; input is never clamped')
            output = duration
        elif kind == 'extend':
            if continuation_only_confirmed is not True:
                raise ValueError('extension requires explicit continuation-only output contract; unknown output shape needs manual review')
            output = finite(extension_sec,'continuation duration')
            if output != int(output) or not 4 <= output <= 30: raise ValueError('continuation must be whole 4–30 seconds')
            start, end = duration, duration + output
        else: raise ValueError('unsupported native repair; regeneration uses its separate preparation route')
        reqs = content_review.requirements(master)
        ids = list(affected_requirement_ids if affected_requirement_ids is not None else [r['id'] for r in reqs])
        if len(ids)!=len(set(ids)) or set(ids)-{r['id'] for r in reqs}: raise ValueError('affected requirement IDs are invalid')
        resolution = ((sb.get('production_settings') or {}).get('generation_snapshot') or {}).get('resolution') or '720p'
        if resolution not in {'480p','720p','1080p'}: raise ValueError('native edit price/support unknown; choose supported resolution or manual route')
        rate = seedance.estimated_cost_rate_cents_per_second(resolution)
        pixels=guards['media']['width']*guards['media']['height']
        input_rate=(68 if pixels<=854*480 else 153 if pixels<=1280*720 else
                    max(344,math.ceil(pixels*344/(1920*1080))))
        output_rate=max(rate,input_rate)  # Adaptive edit/continuation source dimensions are an upper bound.
        estimate = round(duration*input_rate + output*output_rate)
        request = {'kind':kind,'start_sec':start,'end_sec':end,'direction':direction,
            'affected_requirement_ids':ids, 'source_guards':guards, 'resolution':resolution,
            'input_duration_sec':duration, 'provider_output_duration_sec':output,
            'retained_candidate_duration_sec':duration if kind=='edit' else duration+output,
            'provider_output_shape':'full_source_timeline' if kind=='edit' else 'continuation_only',
            'preserved_scope':{'picture':[[0,start],[end,duration]] if kind=='edit' else [[0,duration]],
                               'audio':'selected original native audio; no new TTS', 'unrelated_requirements':[r['id'] for r in reqs if r['id'] not in ids]},
            'estimated_cost_cents':estimate, 'required_cumulative_budget_cents':guards['current_cost_cents']+estimate,
            'estimate_basis':{'input_seconds':duration,'provider_output_seconds':output,
                              'input_rate_cents_per_second':input_rate,'output_rate_cents_per_second':output_rate,
                              'source_pixels':pixels,'area_rate_cents_per_second':output_rate,
                              'basis':'internal conservative calibration; not a guaranteed provider tariff','qa':'configured QA/ASR may incur separate charges'},
            'price_known':True, 'master_idx':int(idx), 'job_id':job_id, 'authority':AUTHORITY,'model':seedance.DEFAULT_MODEL}
        fingerprint = content_review.digest(request)
        if prior:
            if prior['request_fingerprint']==fingerprint: return {**prior,'already_prepared':True}
            raise ValueError('another prepared repair is pending; review/close it before changing source or scope')
        attempt = {**request,'attempt_id':secrets.token_hex(16),'request_fingerprint':fingerprint,
                   'prior_job_state':job['state']}
        event(service.database, job_id, PREPARED, attempt)
        service.database.execute("UPDATE jobs SET state='STORYBOARDED',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=?", (job_id,))
        return {**attempt,'state':'STORYBOARDED','repair_kind':'seedance_extend' if kind=='extend' else 'seedance_visual_edit',
                'next_estimate_cents':estimate,'current_cost_cents':guards['current_cost_cents'],'preparation_only':True}


def verify(database, assets, attempt, *, budget=None, worker=False):
    job, shot, sb, master, qa, path, live = snapshot(database, assets, attempt['job_id'], attempt['master_idx'], attempt['source_guards']['source'])
    expected = attempt['source_guards']
    for key in expected:
        if live[key] != expected[key]: raise StaleRepairError(f'native repair {key} changed; refresh source and prepare again')
    if job['state'] in {'REJECTED','PUBLISHED','REJECTED_PENDING_RECONCILIATION'}: raise StaleRepairError('native repair job terminated')
    if not worker and job['state']!='STORYBOARDED':raise StaleRepairError('prepared repair state changed; review/reconcile current operation')
    if budget is not None and int((sb.get('production_settings') or {}).get('budget_cents') or 0) != budget:
        raise StaleRepairError('native repair budget changed after confirmation')
    if worker and job['state'] not in {'PRODUCTION_QUEUED','PRODUCTION_RUNNING'}:
        raise StaleRepairError('native repair execution state changed; review or reconcile current operation')
    return sb, master, path


def discard_prepared(service,job_id):
    with node_state_machine.atomic(service.database):
        attempt=current(service.database,job_id)
        if not attempt: return {'job_id':job_id,'already_closed':True}
        if service.database.execute('SELECT 1 FROM production_nodes WHERE job_id=? AND node_id=?',(job_id,'native_repair:'+attempt['attempt_id'])).fetchone():
            raise ValueError('started repair must be reconciled and explicitly reviewed; preparation cannot discard paid work')
        event(service.database,job_id,CLOSED,{'attempt_id':attempt['attempt_id'],'decision':'discard_preparation'})
        service.database.execute('UPDATE jobs SET state=? WHERE job_id=? AND state=\'STORYBOARDED\'',(attempt['prior_job_state'],job_id))
        return {'job_id':job_id,'discarded_attempt_id':attempt['attempt_id']}


def authorized(database, node):
    value = content_review.object_json(node['input_json'])
    attempt_id = value.get('attempt',{}).get('attempt_id')
    rows = database.execute('SELECT message FROM events WHERE job_id=? AND step=?', (node['job_id'],AUTHORIZED)).fetchall()
    if not any(content_review.object_json(r['message'])==value for r in rows): raise StaleRepairError('native repair node has no server authorization')
    attempt = value['attempt']
    if node['node_id'] != f'native_repair:{attempt_id}' or node['node_type']!='native_repair' or node['spec_hash']!=attempt['source_guards']['spec_hash'] or node['idempotency_key']!=f"{node['job_id']}:native-repair:{attempt_id}":
        raise StaleRepairError('native repair node identity is forged')
    job = database.execute('SELECT production_spec_hash,production_spec_json FROM jobs WHERE job_id=?',(node['job_id'],)).fetchone()
    if job['production_spec_hash']!=node['spec_hash'] or content_review.digest(content_review.object_json(job['production_spec_json']))!=attempt['source_guards']['spec_digest']:
        raise StaleRepairError('native repair frozen spec changed')
    return value


def graph_errors(database, job_id, spec):
    """Allow only event-authorized additive repair nodes and their exact edge."""
    expected={item['node_id'] for item in spec.get('nodes') or []}
    original_edges={(e['from_node_id'],e['to_node_id'],e['edge_type']) for e in spec.get('edges') or []}
    allowed_edges=set(original_edges)
    errors=[]
    for row in database.execute('SELECT * FROM production_nodes WHERE job_id=?',(job_id,)).fetchall():
        if row['node_id'] in expected and row['node_type']!='native_repair': continue
        try:
            authorized(database,row)
            allowed_edges.add((row['node_id'],'local_compose','native_repair'))
            edges=database.execute('SELECT from_node_id,to_node_id,edge_type FROM production_edges WHERE job_id=? AND (from_node_id=? OR to_node_id=?)',(job_id,row['node_id'],row['node_id'])).fetchall()
            if [tuple(e) for e in edges] != [(row['node_id'],'local_compose','native_repair')]:
                raise StaleRepairError('native repair edge is forged')
        except (ValueError,KeyError,TypeError): errors.append(str(row['node_id']))
    for edge in database.execute("SELECT * FROM production_edges WHERE job_id=? AND edge_type='native_repair'",(job_id,)).fetchall():
        row=database.execute('SELECT * FROM production_nodes WHERE job_id=? AND node_id=?',(job_id,edge['from_node_id'])).fetchone()
        if row is None or row['node_type']!='native_repair' or edge['to_node_id']!='local_compose': errors.append('forged-native-repair-edge')
    for edge in database.execute('SELECT from_node_id,to_node_id,edge_type FROM production_edges WHERE job_id=?',(job_id,)).fetchall():
        if tuple(edge) not in allowed_edges:errors.append('unauthorized-extra-edge')
    return errors


def reconcile(service,job_id,call):
    """Queue only retrieval of a previously owned paid native attempt."""
    usage=content_review.object_json(call['usage_json'])
    node_id=str(usage.get('owner_node_id') or '')
    if not node_id.startswith('native_repair:'): return None
    database=service.database; caller_owns=database.in_transaction
    with node_state_machine.atomic(database):
        job=database.execute('SELECT state FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        if job is None or job['state'] in {'REJECTED','PUBLISHED','REJECTED_PENDING_RECONCILIATION'}:
            raise StaleRepairError('terminated native repair cannot resume graph; reconcile its paid ledger without reviving production')
        node=database.execute('SELECT * FROM production_nodes WHERE job_id=? AND node_id=?',(job_id,node_id)).fetchone()
        if node is None: raise StaleRepairError('native paid recovery owner is missing')
        authorized(database,node)
        if node['provider_task_id']!=call['task_id'] or usage.get('owner_spec_hash')!=node['spec_hash']:
            raise StaleRepairError('native paid recovery ledger ownership mismatch')
        if node['state']=='REVIEW_REQUIRED':return {'job_id':job_id,'state':'PRODUCTION_REVIEW_REQUIRED','already_retrieved':True,'get_only':True}
        if node['state'] not in {'ACTION_REQUIRED','FAILED','READY'}:raise ValueError('native repair is currently owned by a worker; wait for reconciliation')
        if node['state']!='READY':
            node_state_machine.transition_node_in_transaction(database,job_id,node_id,'retry' if node['state']=='FAILED' else 'resume',
                event_id=node_id+':retrieve:'+secrets.token_hex(8),payload={'task_id':call['task_id'],'get_only':True})
        output=content_review.object_json(node['output_json']);output['resume_provider_task_id']=call['task_id']
        database.execute('UPDATE production_nodes SET output_json=?,error_code=NULL,next_action=NULL WHERE job_id=? AND node_id=?',(json.dumps(output),job_id,node_id))
        database.execute("UPDATE jobs SET state='PRODUCTION_QUEUED',production_worker_pid=NULL WHERE job_id=?",(job_id,))
        event(database,job_id,'native_visual_repair_retrieve_queued',{'node_id':node_id,'task_id':call['task_id'],'get_only':True})
    if not caller_owns:service.dispatch_production_queue()
    return {'job_id':job_id,'state':'PRODUCTION_QUEUED','get_only':True,'task_id':call['task_id'],'dispatch_deferred':caller_owns}


def review_node(database, job_id, idx):
    shot=database.execute('SELECT video_qa FROM shots WHERE job_id=? AND idx=?',(job_id,idx)).fetchone()
    qa=content_review.object_json(shot['video_qa']) if shot else {}
    identity=qa.get('native_repair_attempt_id')
    if not identity: return None
    node=database.execute('SELECT * FROM production_nodes WHERE job_id=? AND node_id=?',(job_id,'native_repair:'+identity)).fetchone()
    if node is None: raise StaleRepairError('native candidate has no owned graph attempt')
    authorized(database,node)
    return node


def require_full_candidate(database,node,path):
    """Structural assembly proof cannot be waived by a visual observation."""
    value=authorized(database,node);attempt=value['attempt']
    records=database.execute('SELECT message FROM events WHERE job_id=? AND step=? ORDER BY id DESC',
        (node['job_id'],'native_visual_repair_candidate')).fetchall()
    record=next((content_review.object_json(r['message']) for r in records
        if content_review.object_json(r['message']).get('attempt_id')==attempt['attempt_id']),None)
    if not record or not record.get('full_candidate') or record.get('candidate_sha256')!=visual_evidence.asset_fingerprint(path):
        raise ValueError('native full-candidate assembly proof is missing/stale; manual visual waivers cannot authorize raw output')
    return record


def close_review(database, node, decision, operation_id):
    value=authorized(database,node); attempt=value['attempt']; job_id=attempt['job_id']; idx=attempt['master_idx']
    if decision=='regenerate_master': raise ValueError('native repair regeneration needs a new explicitly prepared scope and budget')
    if decision=='accept_candidate':
        qa=content_review.object_json(database.execute('SELECT video_qa FROM shots WHERE job_id=? AND idx=?',(job_id,idx)).fetchone()[0])
        if not qa.get('native_repair_full_candidate'): raise ValueError('raw provider candidate needs manual assembly reconciliation before acceptance')
        active=database.execute('SELECT video_path FROM shots WHERE job_id=? AND idx=?',(job_id,idx)).fetchone()[0]
        assembled=require_full_candidate(database,node,Path(active))
        duration=assembled['candidate_picture_duration_sec']
        database.execute('UPDATE shots SET duration_sec=? WHERE job_id=? AND idx=?',(duration,job_id,idx))
        live=content_review.object_json(database.execute('SELECT storyboard FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0])
        next(m for m in live['masters'] if m['idx']==idx)['duration']=duration
        database.execute('UPDATE jobs SET storyboard=? WHERE job_id=?',(json.dumps(live,ensure_ascii=False),job_id))
    node_state_machine.transition_node_in_transaction(database,job_id,node['node_id'],'approve',
        event_id=node['node_id']+':review:'+operation_id,payload={'decision':decision,'master_idx':idx})
    original=database.execute('SELECT state FROM production_nodes WHERE job_id=? AND node_id=?',(job_id,f'master:{idx}')).fetchone()
    if original and original['state']=='REVIEW_REQUIRED':
        # Explicit acceptance of fresh full replacement (or proven prior) resolves
        # the origin review without granting the selected bad source approval.
        node_state_machine.transition_node_in_transaction(database,job_id,f'master:{idx}','approve',
            event_id=node['node_id']+':resolve-origin:'+operation_id,payload={'decision':decision,'superseded_by':node['node_id']})
    event(database,job_id,CLOSED,{'attempt_id':attempt['attempt_id'],'decision':decision,'operation_id':operation_id})


def accepted_storyboard(database,job_id,storyboard):
    """Project only event-proven accepted media bounds into local execution."""
    result=deepcopy(storyboard)
    for row in database.execute("SELECT * FROM production_nodes WHERE job_id=? AND node_type='native_repair' AND state='SUCCEEDED' ORDER BY rowid",(job_id,)).fetchall():
        value=authorized(database,row);attempt=value['attempt'];idx=attempt['master_idx']
        closes=database.execute('SELECT message FROM events WHERE job_id=? AND step=? ORDER BY id DESC',(job_id,CLOSED)).fetchall()
        closure=next((content_review.object_json(r['message']) for r in closes if content_review.object_json(r['message']).get('attempt_id')==attempt['attempt_id']),{})
        if closure.get('decision')!='accept_candidate':continue
        shot=database.execute('SELECT video_path,video_qa FROM shots WHERE job_id=? AND idx=?',(job_id,idx)).fetchone()
        if content_review.object_json(shot['video_qa']).get('native_repair_attempt_id')!=attempt['attempt_id']:continue
        record=require_full_candidate(database,row,Path(shot['video_path']))
        next(m for m in result['masters'] if m['idx']==idx)['duration']=record['candidate_picture_duration_sec']
    return result


def start(service, attempt, budget):
    database = service.database
    caller_owns = database.in_transaction
    node_id = 'native_repair:'+attempt['attempt_id']
    with node_state_machine.atomic(database):
        existing = database.execute('SELECT * FROM production_nodes WHERE job_id=? AND node_id=?',(attempt['job_id'],node_id)).fetchone()
        if existing:
            authorized(database,existing)
            return {'job_id':attempt['job_id'],'attempt_id':attempt['attempt_id'],'status':existing['state'],'already_started':True}
        sb, master, path = verify(database, service.data_dir/'assets',attempt)
        service._assert_no_unresolved_provider_tasks(attempt['job_id'])
        if budget is None or int(budget)<attempt['required_cumulative_budget_cents']: raise ValueError(f"repair budget insufficient: requires {attempt['required_cumulative_budget_cents']} cents")
        budget=int(budget)
        sb.setdefault('production_settings',{})['budget_cents']=budget
        snapshot_settings=sb['production_settings'].setdefault('generation_snapshot',{})
        snapshot_settings.update(job_budget_cents=budget,max_auto_retries=0,auto_retry_budget_cents=0)
        value={'attempt':attempt,'approved_budget_cents':budget}
        event(database,attempt['job_id'],AUTHORIZED,value)
        database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,paid,estimated_cost_cents,input_json,spec_hash,idempotency_key) VALUES(?,?,?,'READY',1,?,?,?,?)",
            (attempt['job_id'],node_id,'native_repair',attempt['estimated_cost_cents'],json.dumps(value,ensure_ascii=False),attempt['source_guards']['spec_hash'],f"{attempt['job_id']}:native-repair:{attempt['attempt_id']}"))
        database.execute('INSERT INTO production_edges(job_id,from_node_id,to_node_id,edge_type) VALUES(?,?,?,?)',(attempt['job_id'],node_id,'local_compose','native_repair'))
        # Local nodes alone are invalidated; paid originals and their ledger stay immutable.
        for row in database.execute("SELECT node_id,state FROM production_nodes WHERE job_id=? AND node_id IN ('local_compose','final_review')",(attempt['job_id'],)).fetchall():
            database.execute("UPDATE production_nodes SET state='PENDING',lease_id=NULL,output_json='{}',error_code=NULL WHERE job_id=? AND node_id=?",(attempt['job_id'],row['node_id']))
            database.execute('INSERT INTO production_node_events(job_id,event_id,node_id,event,from_state,to_state,payload_json) VALUES(?,?,?,?,?,?,?)',
                (attempt['job_id'],node_id+':invalidate:'+row['node_id'],row['node_id'],'native_repair_invalidate',row['state'],'PENDING',json.dumps({'attempt_id':attempt['attempt_id']})))
        database.execute("UPDATE jobs SET state='PRODUCTION_QUEUED',storyboard=?,production_worker_pid=NULL,error=NULL WHERE job_id=?",(json.dumps(sb,ensure_ascii=False),attempt['job_id']))
    dispatch = {'dispatched_jobs':[]} if caller_owns else service.dispatch_production_queue()
    return {'job_id':attempt['job_id'],'attempt_id':attempt['attempt_id'],'status':'started' if attempt['job_id'] in dispatch['dispatched_jobs'] else 'queued','dispatch_deferred':caller_owns}


async def assemble(attempt, source, provider_output, output):
    """Keep original audio and unedited picture intervals; never pad picture."""
    raw = json.loads(await ffmpeg._ffprobe(['-show_streams','-of','json',str(provider_output)]))
    stream=next(s for s in raw['streams'] if s.get('codec_type')=='video')
    actual=finite(stream.get('duration'),'provider picture duration')
    original=attempt['input_duration_sec']; start=attempt['start_sec']; end=attempt['end_sec']
    if attempt['kind']=='edit' and actual + .0001 < end:
        raise ValueError('provider picture does not cover the target interval; preserve candidate and manually reconcile, never pad/stretch')
    if attempt['kind']=='extend' and abs(actual-attempt['provider_output_duration_sec'])>.45:
        raise ValueError('unknown continuation output shape; manual review required before assembly')
    width=attempt['source_guards']['media']['width']; height=attempt['source_guards']['media']['height']
    pieces=[]; inputs=[]; chains=[]
    if attempt['kind']=='edit':
        spans=[(source,0,start),(provider_output,start,end),(source,end,original)]
    else: spans=[(source,0,original),(provider_output,0,actual)]
    for path,begin,finish in spans:
        if finish<=begin: continue
        n=len(pieces); inputs += ['-i',str(path)]
        chains.append(f'[{n}:v:0]trim=start={begin}:end={finish},setpts=PTS-STARTPTS,scale={width}:{height},setsar=1,fps=24[v{n}]')
        pieces.append(f'[v{n}]')
    chains.append(''.join(pieces)+f'concat=n={len(pieces)}:v=1:a=0[v]')
    audio_index=len(pieces); inputs+=['-i',str(source)]
    length=original if attempt['kind']=='edit' else original+actual
    args=inputs+['-filter_complex',';'.join(chains),'-map','[v]']
    if attempt['source_guards']['media']['has_audio']:
        args+=['-map',f'{audio_index}:a:0','-af',f'atrim=duration={original},asetpts=PTS-STARTPTS','-c:a','aac']
    args+=['-t',str(length),'-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-movflags','+faststart',str(output)]
    await ffmpeg._run(args)
    return length


async def run(database, node):
    from . import production_executor
    from .steps import s3_shots
    value=authorized(database,node); attempt=value['attempt']; job_id=attempt['job_id']; idx=attempt['master_idx']
    assets=s3_shots.ASSETS_DIR
    recovery=bool(str(node['provider_task_id'] or '').strip())
    if recovery:
        # Historical owned paid task is retrieved before applying new submission rules.
        sb=content_review.object_json(database.execute('SELECT storyboard FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0])
        master=next(m for m in sb['masters'] if m['idx']==idx); source=Path(attempt['source_guards']['source_path'])
    else:
        sb,master,source=verify(database,assets,attempt,budget=value['approved_budget_cents'],worker=True)
        if provider_tasks.unresolved_task_ids(database,job_id): raise StaleRepairError('unresolved provider task requires reconciliation')
    unit={**master,'duration_sec':attempt['input_duration_sec'],'repair_source_path':str(source),
        'repair_start_sec':attempt['start_sec'],'repair_end_sec':attempt['end_sec'],'repair_direction':attempt['direction'],
        'extend_duration_sec':attempt['provider_output_duration_sec'],'extend_direction':attempt['direction'],
        '_generation_snapshot':{'resolution':attempt['resolution'],'max_auto_retries':0}}
    request=s3_shots._extend_request_kwargs(unit) if attempt['kind']=='extend' else s3_shots._lip_sync_edit_request_kwargs(unit)
    request['native_input_rate_cents_per_second']=attempt['estimate_basis']['input_rate_cents_per_second']
    request['native_output_rate_cents_per_second']=attempt['estimate_basis']['output_rate_cents_per_second']
    prompt=s3_shots._build_extend_prompt(unit) if attempt['kind']=='extend' else (
        f"Edit only {attempt['start_sec']:g}–{attempt['end_sec']:g}s: {attempt['direction']}. Preserve identity, performance, camera and all other original content. Original audio is authoritative; no new speech or TTS.")
    output=assets/job_id/'shots'/'review'/'repairs'/f"{idx:02d}-{attempt['attempt_id']}.mp4"
    route=s3_shots.VideoRoute(attempt['model'],'native_repair',attempt['source_guards']['media']['has_audio'],True)
    if 'seedance-2-5' not in route.model: raise ValueError('native edit requires supported Seedance 2.5; use alternate manual route')
    try:
        result=await s3_shots._generate_seedance_video(prompt=prompt,route=route,request_kwargs=request,out_path=output,
            key_hint=f"{job_id}-native-{attempt['attempt_id']}",job_id=job_id,shot_idx=idx,database=database,
            expected_text=master.get('voiceover') if attempt['kind']=='edit' else None,
            require_lip_sync=master.get('type')=='dialogue', require_broll_semantics=master.get('type')=='broll',
            asr_enabled=bool((sb.get('production_settings') or {}).get('asr_enabled',True)),
            generation_snapshot={'resolution':attempt['resolution'],'max_auto_retries':0},
            visual_requirements=content_review.requirements(master), visual_plan=master.get('visual_plan'),
            native_repair={'attempt':attempt,'budget':value['approved_budget_cents'],'node_id':node['node_id']})
        error=s3_shots.SeedanceQualityError('Native repair requires explicit full-candidate review',cost_cents=result.cost_cents,
            provider_calls=result.provider_calls,reasons=('repair_candidate_ready',),candidate_path=result.video_path,candidate_url=result.video_url,
            qa_evidence=content_review.object_json(result.qa_note).get('result') or {},transcription=result.transcription,transcription_available=result.transcription_available)
    except s3_shots.SeedanceQualityError as exc: error=exc
    # The established failure writer retains original artifacts and cumulative costs.
    prior_qa=content_review.object_json(database.execute('SELECT video_qa FROM shots WHERE job_id=? AND idx=?',(job_id,idx)).fetchone()[0])
    full_candidate='native_repair_assembly_required' not in error.reasons
    picture_duration=None
    if full_candidate:
        picture_duration=finite(await ffmpeg._ffprobe(['-select_streams','v:0','-show_entries','stream=duration','-of','csv=p=0',str(error.candidate_path)]),'candidate picture duration')
    s3_shots._record_failed_generation(database,job_id,idx,error)
    with node_state_machine.atomic(database):
        row=database.execute('SELECT video_qa FROM shots WHERE job_id=? AND idx=?',(job_id,idx)).fetchone()
        qa=content_review.object_json(row['video_qa']); qa['native_repair_attempt_id']=attempt['attempt_id']
        qa['native_repair_scope']=attempt
        qa['native_repair_full_candidate']=full_candidate
        qa['native_candidate_picture_duration_sec']=picture_duration
        qa['content_reviews']=prior_qa.get('content_reviews') or []
        qa['prior_candidates']=[*(prior_qa.get('prior_candidates') or []),prior_qa]
        raw_provider=output.with_name(f'..{output.stem}.candidate.provider.mov')
        if raw_provider.is_file():qa['native_raw_provider_path']=str(raw_provider)
        event(database,job_id,'native_visual_repair_candidate',{'attempt_id':attempt['attempt_id'],'qa':qa,
            'full_candidate':qa['native_repair_full_candidate'],
            'candidate_sha256':visual_evidence.asset_fingerprint(Path(error.candidate_path)),
            'candidate_picture_duration_sec':picture_duration,
            'source_audio_authority':attempt['source_guards']['asset_sha256']})
        database.execute('UPDATE shots SET video_qa=? WHERE job_id=? AND idx=?',(json.dumps(qa,ensure_ascii=False),job_id,idx))
    return production_executor.NodeOutcome.require_review({'master_idx':idx,'candidate_path':str(error.candidate_path),
        'attempt_id':attempt['attempt_id'],'status':'manual_review_required'})
