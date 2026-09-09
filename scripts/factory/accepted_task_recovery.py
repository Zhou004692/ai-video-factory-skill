"""Use committed human acceptance evidence to close stale provider ledger rows."""
import hashlib
import json

from . import node_state_machine, production_executor, production_projection, provider_tasks, review_candidates


def recover(database, assets_dir, job_id, task_id):
    with node_state_machine.atomic(database):
        job = database.execute('SELECT state,production_worker_pid FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        if not job or job['state'] != 'PROVIDER_ACTION_REQUIRED' or job['production_worker_pid']:
            return None
        call = database.execute('SELECT * FROM provider_calls WHERE task_id=? AND job_id=?',(task_id,job_id)).fetchone()
        if not call or call['provider'] != 'seedance' or call['shot_idx'] <= 0:
            return None
        shot = database.execute('SELECT * FROM shots WHERE job_id=? AND idx=?',(job_id,call['shot_idx'])).fetchone()
        node = database.execute('SELECT * FROM production_nodes WHERE job_id=? AND node_id=?',(job_id,f"master:{call['shot_idx']}")).fetchone()
        if not shot or shot['state'] != 'OK' or not node or node['state'] != 'SUCCEEDED' or node['provider_task_id'] != task_id:
            return None
        try:
            qa = json.loads(shot['video_qa'] or '{}')
            op = qa.get('review_operation') or {}
            evidence = [r for r in qa.get('provider_calls',[]) if r.get('task_id') == task_id and r.get('cost_cents') == call['cost_cents']]
            if (qa.get('human_decision') != 'accept_candidate' or op.get('decision') != 'accept_candidate'
                    or op.get('phase') != 'committed' or len(evidence) != 1):
                return None
            active = review_candidates.controlled_job_asset_path(assets_dir,job_id,shot['video_path'],require_file=True)
            committed = review_candidates.controlled_job_asset_path(assets_dir,job_id,op.get('active_path'),require_file=True)
            if not active or active != committed or hashlib.sha256(active.read_bytes()).hexdigest() != op.get('candidate_sha256'):
                return None
        except (ValueError,TypeError,AttributeError,OSError):
            return None
        usage = json.loads(call['usage_json'] or '{}')
        usage.update({'status':'completed','recovered_from':'committed_candidate_acceptance','candidate_sha256':op['candidate_sha256']})
        database.execute('UPDATE provider_calls SET usage_json=? WHERE task_id=? AND job_id=?',(json.dumps(usage),task_id,job_id))
        production_executor.release_resolved_provider_gates(database,job_id)
        state = production_projection.persist(database,job_id).projected_job_state
        database.execute('INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)',
                         (job_id,'provider_acceptance_reconcile','info',f'{task_id}: committed acceptance and SHA256 verified; cost unchanged'))
    return {'job_id':job_id,'task_id':task_id,'remote_status':'completed','state':state,'cost_delta_cents':0}
