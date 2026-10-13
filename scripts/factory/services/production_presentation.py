"""Pure UI projection. State and action authority remains ProductionService/recovery_plan."""
from datetime import datetime, timezone
import json
import hashlib


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or '{}')
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _category(code):
    text = str(code or '').casefold()
    if any(x in text for x in ('policy', 'privacy', 'copyright', 'content_rejected')):
        return 'content_policy'
    if 'budget' in text:
        return 'budget'
    if any(x in text for x in ('download', 'retrieve', 'publish_staging')):
        return 'download'
    if any(x in text for x in ('account', 'balance', 'quota', 'credential', 'authentication')):
        return 'account'
    if any(x in text for x in ('network', 'timeout', 'connection', 'submission_unknown', 'action_required')):
        return 'network_unknown'
    if any(x in text for x in ('review', 'quality', 'retention', 'lip_sync', 'identity')):
        return 'quality'
    return 'awaiting_verification'


def present_production(evidence):
    """Project one consistent snapshot, with no database/filesystem/network access."""
    nodes = evidence.get('nodes') or []
    shots = evidence.get('shots') or []
    calls = evidence.get('provider_calls') or []
    state = str(evidence.get('state') or 'UNKNOWN')
    receipts = evidence.get('submission_receipts') or []
    queue_evidence = bool(nodes or calls or receipts or evidence.get('lease_valid'))
    queued = int(state == 'PRODUCTION_QUEUED' and queue_evidence)
    leased = evidence.get('lease_valid') is True
    running = sum(n.get('state') == 'RUNNING' and n.get('lease_valid', leased) is True for n in nodes)
    if not nodes and state == 'PRODUCTION_RUNNING' and leased:
        running = 1
    remote_ids, unknown_ids = set(), set()
    provider_evidence = []
    errors = []
    for node in nodes:
        output = _object(node.get('output_json'))
        code = output.get('stable_code') or output.get('failure_code') or node.get('error_code')
        if code:
            errors.append({'code': code, 'category': _category(code),
                           'target': {'job_id': evidence.get('job_id'), 'node_id': node.get('node_id')},
                           'source': 'node', 'message': str(output.get('error') or '')})
    for call in calls:
        usage = _object(call.get('usage_json'))
        status = str(usage.get('status') or '').casefold()
        task_id = str(usage.get('remote_task_id') or call.get('task_id') or '')
        if task_id and not task_id.startswith('local-submit:'):
            provider_evidence.append({'task_id': task_id, 'status': status or 'unknown'})
        if status in {'accepted', 'running', 'submitted', 'pending', 'queued', 'processing'} and task_id and not task_id.startswith('local-submit:'):
            remote_ids.add(task_id)
        elif status in {'unknown', 'action_required', 'submitting', 'submission_unknown', 'staged'}:
            unknown_ids.add(task_id)
        code = usage.get('failure_code') or usage.get('error_code')
        message = str(usage.get('message') or '')
        if not code and 'missing downloadable' in message.casefold():
            code = 'provider_download_missing'
        if code:
            errors.append({'code': code, 'category': _category(code), 'source': 'provider',
                           'message': message,
                           'target': {'job_id': evidence.get('job_id'), 'task_id': task_id}})
    for shot in shots:
        qa = _object(shot.get('video_qa'))
        code = qa.get('failure_code')
        if code:
            errors.append({'code': code, 'category': _category(code), 'source': 'shot',
                           'target': {'job_id': evidence.get('job_id'), 'master_idx': shot.get('idx')}})
    # Specific current node/ledger evidence precedes a historic parent failure.
    if not errors and evidence.get('failure_code'):
        code = evidence['failure_code']
        errors.append({'code': code, 'category': _category(code), 'source': 'job',
                       'target': {'job_id': evidence.get('job_id')}})
    categories = {item['category'] for item in errors}
    category = next(iter(categories)) if len(categories) == 1 else 'awaiting_verification'
    masters = [node for node in nodes if node.get('node_type') == 'master']
    total = len(masters) or len(shots) or int(evidence.get('production_total') or 0)
    completed = sum(shot.get('state') == 'OK' and shot.get('media_exists') is True for shot in shots)
    if masters:
        successful = {str(n.get('node_id')) for n in masters if n.get('state') == 'SUCCEEDED'}
        completed = sum(s.get('state') == 'OK' and s.get('media_exists') is True
                        and f"master:{s.get('idx')}" in successful for s in shots)
    blockers = []
    def block(code, message, target=None):
        blockers.append({'code': code, 'message': message, 'target': target or {'job_id': evidence.get('job_id')}})
    if not total or completed < total:
        block('media_incomplete', '可用母片尚未全部完成')
    for node in nodes:
        if node.get('state') in {'FAILED', 'ACTION_REQUIRED', 'BUDGET_REQUIRED', 'REVIEW_REQUIRED'}:
            block(node.get('error_code') or str(node.get('state')).lower(), '节点需要处理',
                  {'job_id': evidence.get('job_id'), 'node_id': node.get('node_id')})
        elif node.get('node_type') != 'final_review' and node.get('state') in {'PENDING', 'READY', 'RUNNING'}:
            block('dependency_incomplete', '制作节点尚未完成',
                  {'job_id': evidence.get('job_id'), 'node_id': node.get('node_id')})
    if unknown_ids:
        block('submission_unknown', '远端提交尚未核对，禁止重复提交')
    if remote_ids:
        block('remote_pending', '远端任务尚未结束')
    if evidence.get('anchor_blocked'):
        block('anchor_review_required', '人物或场景锚点尚未通过审核')
    if evidence.get('content_blocked'):
        block('content_review_required', '画面内容尚未通过审核')
    if state in {'COMPOSED', 'READY_TO_PUBLISH', 'PUBLISHED'}:
        if not evidence.get('final_exists'):
            block('final_media_missing', '成片文件不可用')
        if not evidence.get('final_review_approved'):
            block('final_review_required', '成片验收尚未通过')
    if state in {'FAILED', 'ACCOUNT_ACTION_REQUIRED', 'BUDGET_CONFIRMATION_REQUIRED', 'RECOVERY_REQUIRED', 'PROVIDER_ACTION_REQUIRED', 'REJECTED', 'REJECTED_PENDING_RECONCILIATION'} and not blockers:
        block('job_blocked', evidence.get('error') or '任务需要处理')
    unknown = sum(n.get('state') == 'RUNNING' and not n.get('lease_valid', leased) for n in nodes)
    if not nodes and state == 'PRODUCTION_RUNNING' and not leased and not remote_ids:
        unknown = 1
    if state == 'PRODUCTION_QUEUED' and not queue_evidence:
        unknown = 1
    paused = state in {'FAILED', 'BLOCKED', 'ACCOUNT_ACTION_REQUIRED', 'BUDGET_CONFIRMATION_REQUIRED',
                      'PROVIDER_ACTION_REQUIRED', 'RECOVERY_REQUIRED', 'REJECTED', 'REJECTED_PENDING_RECONCILIATION'}
    parent_review = state in {'ANCHOR_REVIEW_REQUIRED', 'PRODUCTION_REVIEW_REQUIRED', 'COMPOSED'}
    status = ('awaiting_verification' if len(categories) > 1 else
              'review_required' if parent_review else
              'blocked' if paused else
              'running' if running or remote_ids else 'queued' if queued else
              'awaiting_verification' if unknown or unknown_ids else
              'planned' if state in {'STORYBOARDED', 'PENDING'} else
              'review_required' if any('review' in b['code'] for b in blockers) else
              'blocked' if blockers and state not in {'STORYBOARDED', 'PENDING'} else
              'ready' if not blockers else 'planned')
    labels = {'running': '正在制作', 'queued': '等待执行', 'awaiting_verification': '状态待核对',
              'review_required': '等待审核', 'blocked': '需要处理', 'ready': '已通过当前阶段', 'planned': '等待制作'}
    parent_labels = {'ACCOUNT_ACTION_REQUIRED': '账号需要处理', 'BUDGET_CONFIRMATION_REQUIRED': '预算等待确认',
                     'PROVIDER_ACTION_REQUIRED': '远端任务需要核对', 'ANCHOR_REVIEW_REQUIRED': '人物与场景等待审核',
                     'PRODUCTION_REVIEW_REQUIRED': '母片或画面内容等待复核', 'COMPOSED': '成片等待验收',
                     'REJECTED': '任务已终止', 'REJECTED_PENDING_RECONCILIATION': '任务已终止，费用等待核对'}
    task_ids = sorted({c['task_id'] for c in provider_evidence})
    accepted = bool(receipts or task_ids)
    receipt_id = hashlib.sha256(json.dumps({'receipts': sorted(r['id'] for r in receipts),
        'provider_task_ids': task_ids},
        sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest() if accepted else None
    return {'schema_version': 1, 'status': status, 'label': parent_labels.get(state, labels[status]),
            'activity_note': '已受理任务仍在处理' if remote_ids and (paused or parent_review) else '',
            'evidence_status': 'unknown' if not nodes and not calls and evidence.get('execution_engine') == 'legacy' else 'observed',
            'activity': {'queued': queued, 'running': running, 'remote_accepted': len(remote_ids), 'unknown': unknown},
            'plan': {'passed': int(evidence.get('plan_passed') or 0), 'total': int(evidence.get('plan_total') or 0)},
            'production': {'completed': completed, 'total': total},
            'gate': {'can_advance': not blockers, 'blockers': blockers},
            'submission_known': accepted and state not in {'STORYBOARDED', 'PENDING'}
                and not unknown_ids and not unknown and not remote_ids,
            'submission_evidence': {'receipt_id': receipt_id, 'receipts': receipts,
                                    'provider_tasks': provider_evidence, 'accepted': accepted},
            'next_action': evidence.get('next_action'),
            'allowed_actions': evidence.get('allowed_actions') or [],
            'disabled_actions': ([{'id': 'start_video_job', 'target': {'job_id': evidence.get('job_id')},
                'reason': '远端提交尚未核对，禁止重复生成', 'code': 'submission_unknown'}]
                if unknown_ids or remote_ids or unknown else []),
            'error_summary': {'category': category if errors else ('network_unknown' if unknown_ids else None),
                              'causes': errors, 'conflicting_evidence': len(categories) > 1,
                              'allowed_actions': evidence.get('allowed_actions') or []},
            'observed_at': evidence.get('observed_at') or datetime.now(timezone.utc).isoformat()}
