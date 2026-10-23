"""Shared, read-only recovery decisions for the UI and command boundary.

Provider ledger and frozen nodes decide what is safe. Job/shot projections
cannot make a paid retry eligible on their own. Legacy jobs retain their path.
"""
import json
from pathlib import Path

from . import master_download_recovery, network_recovery, provider_tasks

TERMINAL = frozenset({'REJECTED', 'REJECTED_PENDING_RECONCILIATION', 'PUBLISHED'})
LABELS = {
    'reconcile_provider_task': '核对远端任务并恢复产物',
    'reconcile_rejected_provider_cost': '核对终止任务成本',
    'review_master_candidates': '预览并处理候选母片',
    'review_continuity_anchors': '审核人物与场景锚点',
    'retry_network_failures': '重试未提交的生成项',
    'increase_job_budget': '确认剩余生成预算',
    'preserve_and_diagnose': '查看阻塞原因与修复建议',
    'view_progress': '查看生成进度',
    'resume_failed_video': '继续生成剩余母片',
    'confirm_budget': '继续生成剩余母片',
    'review_visual_content': '复核画面内容并恢复本地合成',
}


def _object(raw):
    try:
        parsed = json.loads(raw or '{}')
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resolve(database, job_id):
    # One consistent snapshot across job, graph and provider ledger reads.
    database.execute('SAVEPOINT recovery_snapshot')
    try:
        return _resolve(database, job_id)
    finally:
        database.execute('RELEASE SAVEPOINT recovery_snapshot')


def _resolve(database, job_id):
    job = database.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    if not job or job['execution_engine'] != 'graph_v2':
        return None
    nodes = database.execute('SELECT * FROM production_nodes WHERE job_id=?', (job_id,)).fetchall()
    if not nodes:
        return None
    state = str(job['state'])
    states = {r['state'] for r in nodes}
    unresolved = provider_tasks.unresolved_task_ids(database, job_id)

    def result(ids, reason, **extra):
        actions = [{'id': key, 'label': LABELS[key]} for key in ids]
        return {'action_ids': ids, 'allowed_actions': actions,
                'next_action': actions[0] if actions else None, 'reason': reason,
                'candidate_decisions': (['accept_candidate', 'keep_prior', 'reject_job']
                    + ([] if unresolved else ['regenerate_master'])) if 'review_master_candidates' in ids else [], **extra}

    if state in TERMINAL:
        return result(['reconcile_rejected_provider_cost'] if state == 'REJECTED_PENDING_RECONCILIATION' else [], '任务已终止或发布')
    if state in {'PRODUCTION_RUNNING', 'PRODUCTION_QUEUED'}:
        return result(['view_progress'], '后台正在处理，请等待当前操作结束')
    review_nodes = [r for r in nodes if r['node_type'] == 'master' and r['state'] == 'REVIEW_REQUIRED']
    candidate_reviews = []
    for node in review_nodes:
        shot = database.execute('SELECT state,video_qa FROM shots WHERE job_id=? AND idx=?', (job_id, int(node['node_id'].split(':')[1]))).fetchone()
        qa = _object(shot['video_qa']) if shot else {}
        candidate = str(qa.get('candidate_path') or '')
        if shot and shot['state'] in {'FAILED', 'REVIEW_REQUIRED'} and qa.get('status') == 'manual_review_required' and candidate and Path(candidate).is_file():
            candidate_reviews.append(node['node_id'])
    review_actions = ['review_master_candidates'] if candidate_reviews else []
    if unresolved:
        if state == 'FAILED' and str(job['failure_code'] or '') in {
            'input_privacy_rejected', 'output_policy_rejected',
            'output_copyright_rejected',
        }:
            return result(['preserve_and_diagnose'],
                          '内容政策失败需要人工诊断；远端费用记录仍须核查，不能自动恢复生成',
                          task_ids=unresolved)
        return result(['reconcile_provider_task', *review_actions], '远端提交或产物尚未核对，保留已有候选，禁止重复生成', task_ids=unresolved)
    content_pause = any(r['node_id'] == 'local_compose' and r['state'] == 'REVIEW_REQUIRED'
        and r['error_code'] == 'content_review_required' for r in nodes)
    if content_pause and all(r['state'] == 'SUCCEEDED' for r in nodes if r['node_type'] == 'master'):
        return result(['review_visual_content'], '画面内容需要人工复核；仅恢复本地合成，不重新生成母片或配乐', recovery_kind='visual_content')
    if 'RUNNING' in states:
        return result(['view_progress'], '后台节点仍在运行；如进度未变化，请执行过期租约恢复扫描')
    retry = network_recovery.retry_indexes(database, job_id)
    anchors = network_recovery.retry_anchor_ids(database, job_id)
    if retry or anchors:
        return result(['retry_network_failures', *review_actions], '仅重试已确认未提交的连接失败，保留已有锚点与母片', retry_indexes=retry, retry_anchor_ids=anchors)
    if candidate_reviews:
        return result(review_actions, '已有母片等待人工判断；接受或重做后重新判定剩余工作')
    # A recovered anchor remains unapproved while unpaid siblings are ready.
    # Let the user return to budget preflight without accepting that anchor or
    # granting any master candidate a retry/approval.
    if (state == 'PRODUCTION_REVIEW_REQUIRED' and not job['production_worker_pid']
            and not states.intersection({'FAILED','ACTION_REQUIRED','BUDGET_REQUIRED'})
            and any(r['node_type'] in {'character_anchor','scene_anchor'} and r['state']=='READY' for r in nodes)
            and any(r['node_type'] in {'character_anchor','scene_anchor'} and r['state']=='REVIEW_REQUIRED' for r in nodes)
            and not review_nodes
            and all(r['node_type'] in {'character_anchor','scene_anchor'} for r in nodes if r['state']=='READY')):
        plan = result(['resume_failed_video'], '已取回人物或场景候选；可确认预算后继续生成其余未提交锚点，候选保持待审核', recovery_kind='unsubmitted_anchors')
        plan['next_action']['label'] = '继续准备剩余人物与场景'
        return plan
    download_recovery = master_download_recovery.assess(database, job_id)
    if download_recovery is not None:
        if download_recovery.eligible:
            return result(
                ['resume_failed_video'],
                download_recovery.reason,
                recovery_kind='completed_master_download',
                retry_indexes=list(download_recovery.indexes),
            )
        return result(['preserve_and_diagnose'], download_recovery.reason)
    if state == 'ANCHOR_REVIEW_REQUIRED':
        return result(['review_continuity_anchors'], '人物或场景锚点等待审核')
    if 'BUDGET_REQUIRED' in states or state == 'BUDGET_CONFIRMATION_REQUIRED':
        return result(['increase_job_budget'], '剩余生成需要确认预算')
    if state == 'RECOVERY_REQUIRED':
        return result(['preserve_and_diagnose'], '当前没有可执行节点，请检查依赖与恢复记录')
    failures = [r for r in nodes if r['node_type'] == 'master' and r['state'] == 'FAILED']
    if failures:
        node = failures[0]
        row = database.execute("SELECT payload_json FROM production_node_events WHERE job_id=? AND node_id=? AND event='fail' ORDER BY created_at DESC,rowid DESC LIMIT 1", (job_id, node['node_id'])).fetchone()
        detail = _object(row[0] if row else None).get('error') or node['error_code'] or '请检查执行条件'
        return result(['preserve_and_diagnose'], f"母片 {node['node_id']} 的执行失败尚未修复：{detail}")
    if review_nodes or 'ACTION_REQUIRED' in states:
        return result(['preserve_and_diagnose'], '执行记录仍有待处理阻塞，请核对候选、依赖与远端记录')
    # Preserve existing specialized account, anchor, local-post and final-review
    # flows when there is no master-level conflict to override them.
    if 'FAILED' in states:
        return None
    if state in {'FAILED', 'PROVIDER_ACTION_REQUIRED'} and not job['failure_code']:
        shot_codes = [_object(r[0]).get('failure_code') for r in database.execute(
            "SELECT video_qa FROM shots WHERE job_id=? AND state!='OK'", (job_id,),
        )]
        if any(shot_codes):
            return None
        if 'READY' in states:
            return result(['resume_failed_video'], '执行条件已具备，保留已完成母片继续处理')
        if 'PENDING' in states and not all(r['state'] == 'SUCCEEDED' for r in nodes if r['node_type'] == 'master'):
            return result(['preserve_and_diagnose'], '还有等待中的母片，但没有可执行节点，请检查依赖状态')
    if state == 'STORYBOARDED' and any(r['node_type'] == 'master' and r['state'] == 'SUCCEEDED' for r in nodes):
        return result(['confirm_budget'], '已完成母片保留，确认剩余成本后继续')
    return None


def require_action(database, job_id, action):
    plan = resolve(database, job_id)
    if plan is not None and action not in plan['action_ids']:
        raise ValueError(plan['reason'])
    return plan


def require_review_decision(database, job_id, decision):
    plan = require_action(database, job_id, 'review_master_candidates')
    if plan is not None and decision not in plan['candidate_decisions']:
        raise ValueError('远端提交尚未核对，暂不能重做；可先接受已有候选')
    return plan


def resume_ready_graph(database, job_id):
    """Atomically admit ready graph work while preserving paid projections."""
    import uuid
    from . import node_state_machine, state_machine
    with node_state_machine.atomic(database):
        plan = require_action(database, job_id, 'resume_failed_video')
        if plan is None:
            raise ValueError('任务恢复条件已变化，请刷新后重试')
        database.execute(
            "UPDATE shots SET state='PENDING',error=NULL WHERE job_id=? AND state!='OK' "
            "AND EXISTS(SELECT 1 FROM production_nodes n WHERE n.job_id=shots.job_id "
            "AND n.node_id='master:'||shots.idx AND n.state='READY')", (job_id,),
        )
        event = 'resume_anchor_generation' if plan.get('recovery_kind')=='unsubmitted_anchors' else 'resume_generation'
        state_machine.transition(database, job_id, event,
            event_id=f'{job_id}:resume-graph:{uuid.uuid4().hex}',
            updates={'error': None, 'failure_code': None, 'retries': 0})
        shots = database.execute('SELECT state FROM shots WHERE job_id=?', (job_id,)).fetchall()
        completed = sum(r[0] == 'OK' for r in shots)
        return {'job_id': job_id, 'state': 'STORYBOARDED', 'preserved_masters': completed,
                'remaining_masters': len(shots) - completed, 'manual_review_masters': 0}
