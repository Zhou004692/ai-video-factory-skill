"""Recover a known Seedance anchor task without submitting another generation."""
import asyncio
import hashlib
import json
from pathlib import Path
import uuid

from . import node_state_machine, production_adapters, production_projection, provider_tasks
from .clients import ffmpeg, tos


def anchor_row(database, node):
    data = json.loads(node['input_json'])
    if node['node_type'] == 'scene_anchor':
        table, column, key = 'scene_visual_anchors', 'anchor_key', data['scene_key']
    elif node['node_type'] == 'character_anchor':
        table, column, key = 'character_identity_anchors', 'character_id', data['character_id']
    else:
        raise ValueError('Provider task does not belong to an anchor')
    rows = database.execute(f'SELECT * FROM {table} WHERE job_id=? AND {column}=?',
                            (node['job_id'], key)).fetchall()
    if len(rows) != 1:
        raise ValueError('Anchor ownership is missing or ambiguous')
    return table, column, key, rows[0]


def reconcile(database, data_dir, job_id, task_id, inspected):
    rows = database.execute('SELECT * FROM production_nodes WHERE job_id=? AND provider_task_id=?',
                            (job_id, task_id)).fetchall()
    if len(rows) != 1:
        raise ValueError('Anchor task has no unique frozen node owner')
    node = rows[0]
    spec, node_input = production_adapters._frozen_spec(database, node)
    frozen = [n for n in spec.get('nodes',[]) if n.get('node_id') == node['node_id']]
    if len(frozen) != 1 or frozen[0].get('node_type') != node['node_type'] or frozen[0].get('input') != node_input:
        raise ValueError('Anchor owner differs from frozen node input')
    table, column, key, anchor = anchor_row(database, node)
    call = database.execute('SELECT * FROM provider_calls WHERE job_id=? AND task_id=?',
                            (job_id, task_id)).fetchone()
    expected_provider = 'seedance_scene' if node['node_type'] == 'scene_anchor' else 'seedance_identity'
    if not call or call['provider'] != expected_provider or node['state'] != 'ACTION_REQUIRED' or node['lease_id']:
        raise ValueError('Anchor task ownership or recovery state changed')
    job = database.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
    if job['state'] != 'PROVIDER_ACTION_REQUIRED' or job['production_worker_pid']:
        raise ValueError('Anchor job is not idle for reconciliation')
    status = str(inspected.get('status') or 'unknown')
    if status not in {'completed','failed'} or (status == 'completed' and not inspected.get('video')):
        provider_tasks.mark_action_required(database,task_id,message=f'anchor remote status: {status}')
        return {'job_id':job_id,'task_id':task_id,'remote_status':'unknown' if status == 'completed' else status,
                'state':'PROVIDER_ACTION_REQUIRED'}

    output = {}
    if status == 'completed':
        video = inspected['video']
        if not isinstance(video,(bytes,bytearray)):
            raise ValueError('Invalid downloaded anchor bytes')
        # Unique immutable paths: a crash or losing concurrent request leaves
        # unreferenced media, never overwrites the currently approved artifact.
        asset_root = (Path(data_dir) / 'assets').resolve()
        directory = (asset_root / job_id / 'anchor_recovery' / uuid.uuid4().hex).resolve()
        if not directory.is_relative_to(asset_root):
            raise ValueError('Invalid anchor asset location')
        directory.mkdir(parents=True)
        video_path = directory / 'anchor.mp4'
        video_path.write_bytes(video)
        frames = asyncio.run(ffmpeg.extract_frames(video_path,directory / 'preview',[1.0]))
        if not frames or not Path(frames[0]).is_file() or not Path(frames[0]).stat().st_size:
            raise RuntimeError('Recovered anchor has no decodable preview')
        image_path = Path(frames[0])
        token = hashlib.sha256(f'{job_id}:{task_id}'.encode()).hexdigest()[:24]
        video_url = tos.upload(video_path,key_hint=f'{token}-recovered-video')
        image_url = tos.upload(image_path,key_hint=f'{token}-recovered-preview')
        output = {'video_path':str(video_path),'image_path':str(image_path),
                  'video_url':video_url,'image_url':image_url,'task_id':task_id}

    with node_state_machine.atomic(database):
        current_job = database.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        current_node = database.execute('SELECT * FROM production_nodes WHERE job_id=? AND node_id=?',
                                        (job_id,node['node_id'])).fetchone()
        current_call = database.execute('SELECT * FROM provider_calls WHERE task_id=?',(task_id,)).fetchone()
        current_anchor = anchor_row(database,current_node)[3]
        if any(dict(before) != dict(after) for before,after in
               [(job,current_job),(node,current_node),(call,current_call),(anchor,current_anchor)]):
            raise ValueError('Anchor reconciliation changed concurrently; refresh and retry')
        delta = provider_tasks.upsert_final_call(database,task_id=task_id,job_id=job_id,
            shot_idx=call['shot_idx'],provider=call['provider'],intent=call['intent'],status=status,
            usage=inspected.get('usage') or {},cost_cents=int(inspected.get('cost_cents') or 0))
        # Download failures can have ledger/node cost already recorded while
        # the anchor and job have not received it. Reconcile both projections.
        total = max(int(node['actual_cost_cents'] or 0), int(call['cost_cents'] or 0)) + delta
        job_delta = max(0,total - int(anchor['cost_cents'] or 0))
        if status == 'completed':
            database.execute(f"UPDATE {table} SET state='READY',approval_state='PENDING_REVIEW',"
                f"approved_at=NULL,review_note='',video_path=?,video_url=?,image_path=?,image_url=?,"
                f"preview_path=?,cost_cents=cost_cents+?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND {column}=?",
                (output['video_path'],output['video_url'],output['image_path'],output['image_url'],
                 output['image_path'],job_delta,job_id,key))
            if table == 'character_identity_anchors':
                database.execute("UPDATE character_identity_anchors SET anchor_mode='dynamic_video',duration_sec=4 WHERE job_id=? AND character_id=?",(job_id,key))
            node_state_machine.transition_node_in_transaction(database,job_id,node['node_id'],'reconcile_review',
                event_id=f'{job_id}:{task_id}:anchor-reconciled',payload=output)
        else:
            database.execute(f"UPDATE {table} SET cost_cents=cost_cents+?,error='远端锚点生成失败，需检查供应商错误' WHERE job_id=? AND {column}=?",(job_delta,job_id,key))
            node_state_machine.transition_node_in_transaction(database,job_id,node['node_id'],'reconcile_failed',
                event_id=f'{job_id}:{task_id}:anchor-failed',payload={'task_id':task_id})
        database.execute('UPDATE production_nodes SET actual_cost_cents=?,output_json=?,error_code=?,next_action=? WHERE job_id=? AND node_id=?',
            (total,json.dumps(output,ensure_ascii=False),None if status=='completed' else 'provider_generation_failed',
             'review_candidate' if status=='completed' else 'preserve_and_diagnose',job_id,node['node_id']))
        next_state = production_projection.reconcile(database,job_id).projected_job_state
        database.execute('UPDATE jobs SET state=?,cost_cents=cost_cents+?,error=NULL,failure_code=NULL,updated_at=CURRENT_TIMESTAMP WHERE job_id=?',
                         (next_state,job_delta,job_id))
        database.execute('INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)',
                         (job_id,'anchor_reconcile','info',f'{task_id}: {status}; no generation submitted'))
    return {'job_id':job_id,'task_id':task_id,'remote_status':status,'state':next_state,'cost_delta_cents':job_delta}
