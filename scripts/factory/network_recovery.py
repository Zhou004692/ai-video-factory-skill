"""Recover proven pre-submit connection failures on the existing frozen graph."""
import json
import secrets

from . import node_state_machine, production_adapters, production_invariants
from . import provider_health, provider_tasks, state_machine


def retry_indexes(database, job_id):
    job = database.execute(
        "SELECT state,execution_engine,production_worker_pid FROM jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if not job or job['state'] != 'FAILED' or job['execution_engine'] != 'graph_v2' or job['production_worker_pid']:
        return []
    if provider_tasks.unresolved_task_ids(database, job_id):
        return []
    if provider_health.get_status(database, 'seedance')['status'] != provider_health.AVAILABLE:
        return []
    if database.execute("SELECT 1 FROM production_nodes WHERE job_id=? AND state='RUNNING' LIMIT 1", (job_id,)).fetchone():
        return []
    if production_invariants.audit(database, job_id).blockers:
        return []
    eligible = []
    for shot in database.execute("SELECT * FROM shots WHERE job_id=? AND state='FAILED' ORDER BY idx", (job_id,)):
        try:
            qa = json.loads(shot['video_qa'] or '{}')
        except (TypeError, ValueError):
            continue
        if not isinstance(qa, dict) or qa.get('failure_code') != 'transient_network':
            continue
        # Older rows predate the structured marker. Only the exact error emitted
        # for ConnectError/ConnectTimeout proves the POST never reached a server.
        legacy_pre_submit = str(shot['error'] or '').startswith(
            'SeedanceSubmissionUnknownError: Seedance 提交前连接失败；'
        )
        if qa.get('submit_result_unknown') is not False and not (
            'submit_result_unknown' not in qa and legacy_pre_submit
        ):
            continue
        if shot['cost_cents'] or shot['video_path'] or qa.get('candidate_path') or qa.get('provider_calls'):
            continue
        node = database.execute("SELECT * FROM production_nodes WHERE job_id=? AND node_id=?", (job_id, f"master:{shot['idx']}")).fetchone()
        if not node or node['state'] != 'FAILED' or node['provider_task_id'] or node['actual_cost_cents']:
            continue
        if database.execute("SELECT 1 FROM provider_calls WHERE job_id=? AND shot_idx=? LIMIT 1", (job_id, shot['idx'])).fetchone():
            continue
        if database.execute(
            "SELECT 1 FROM production_edges e JOIN production_nodes n ON n.job_id=e.job_id AND n.node_id=e.from_node_id "
            "WHERE e.job_id=? AND e.to_node_id=? AND n.state!='SUCCEEDED' LIMIT 1",
            (job_id, node['node_id']),
        ).fetchone():
            continue
        try:
            production_adapters._frozen_spec(database, node)
        except production_adapters.FrozenSpecMismatchError:
            continue
        eligible.append(int(shot['idx']))
    return eligible


def retry_anchor_ids(database, job_id):
    from .anchor_recovery import anchor_row
    job = database.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
    if not job or job['state'] != 'FAILED' or job['execution_engine'] != 'graph_v2' or job['production_worker_pid']:
        return []
    if provider_tasks.unresolved_task_ids(database,job_id) or provider_health.get_status(database,'seedance')['status'] != provider_health.AVAILABLE:
        return []
    if database.execute("SELECT 1 FROM production_nodes WHERE job_id=? AND state='RUNNING'",(job_id,)).fetchone() or production_invariants.audit(database,job_id).blockers:
        return []
    eligible = []
    for node in database.execute("SELECT * FROM production_nodes WHERE job_id=? AND node_type IN ('scene_anchor','character_anchor') AND state='FAILED' ORDER BY node_id",(job_id,)):
        if node['provider_task_id'] or node['actual_cost_cents'] or node['lease_id']:
            continue
        try:
            output = json.loads(node['output_json'] or '{}')
            legacy = node['error_code'] == 'SeedanceSubmissionUnknownError' and str(output.get('error','')).startswith('Seedance 提交前连接失败；')
            proven = output.get('submit_result_unknown') is False and output.get('stable_code') == 'transient_network'
            if not proven and not ('submit_result_unknown' not in output and legacy):
                continue
            production_adapters._frozen_spec(database,node)
            anchor = anchor_row(database,node)[3]
        except (ValueError,KeyError,TypeError,production_adapters.FrozenSpecMismatchError):
            continue
        if anchor['cost_cents'] or anchor['video_path'] or anchor['image_path'] or anchor['state'] != 'FAILED':
            continue
        if database.execute("SELECT 1 FROM production_edges e JOIN production_nodes n ON n.job_id=e.job_id AND n.node_id=e.from_node_id WHERE e.job_id=? AND e.to_node_id=? AND n.state!='SUCCEEDED'",(job_id,node['node_id'])).fetchone():
            continue
        eligible.append(node['node_id'])
    return eligible


def queue_retry(database, job_id, *, confirmed):
    if confirmed is not True:
        raise ValueError('请确认重试失败母片；成功提交后会按原任务预算计费')
    owns_transaction = not database.in_transaction
    database.execute('BEGIN IMMEDIATE' if owns_transaction else 'SAVEPOINT network_recovery')
    try:
        from .recovery_plan import require_action
        require_action(database, job_id, "retry_network_failures")
        indexes = retry_indexes(database, job_id)
        anchors = retry_anchor_ids(database, job_id)
        if not indexes and not anchors:
            raise ValueError('当前没有可安全重试的提交前连接失败母片，请刷新任务状态')
        operation = secrets.token_hex(12)
        for node_id in anchors:
            node_state_machine.transition_node_in_transaction(database,job_id,node_id,'retry',
                event_id=f'{job_id}:network-retry:{operation}:{node_id}',payload={'reason':'confirmed_pre_submit_connection_retry'})
            database.execute('UPDATE production_nodes SET error_code=NULL,next_action=NULL WHERE job_id=? AND node_id=?',(job_id,node_id))
        for idx in indexes:
            node_state_machine.transition_node_in_transaction(
                database, job_id, f'master:{idx}', 'retry',
                event_id=f'{job_id}:network-retry:{operation}:{idx}',
                payload={'reason':'confirmed_pre_submit_connection_retry'},
            )
            database.execute("UPDATE production_nodes SET error_code=NULL,next_action=NULL WHERE job_id=? AND node_id=?", (job_id, f'master:{idx}'))
            database.execute("UPDATE shots SET state='PENDING',error=NULL,video_qa=NULL WHERE job_id=? AND idx=?", (job_id, idx))
        state_machine.transition(
            database, job_id, 'queued', event_id=f'{job_id}:network-retry:{operation}',
            updates={'error':None, 'failure_code':None, 'retries':0},
            payload={'retried_master_indexes':indexes, 'retried_anchor_ids':anchors, 'preserve_candidates':True},
        )
        if owns_transaction:
            database.commit()
        else:
            database.execute('RELEASE SAVEPOINT network_recovery')
        return indexes
    except Exception:
        if owns_transaction:
            database.rollback()
        else:
            database.execute('ROLLBACK TO SAVEPOINT network_recovery')
            database.execute('RELEASE SAVEPOINT network_recovery')
        raise
