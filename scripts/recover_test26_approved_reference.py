"""Rehearse Test26's stale reference-limit failure; --apply backs up first.

No provider submission. Real adapter/S3 request construction runs on a copy;
only a matching, unpaid historical failure can then return to budget confirmation.
"""
import argparse
import asyncio
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest import mock

from factory import db, node_state_machine, production_adapters, production_config
from factory import production_invariants, provider_tasks, recovery_plan
from factory.management_api import ManagementService
from factory.steps import s3_shots

JOB = 'J-TOPIC-24f362fa1c315927'


def fingerprint(c):
    values = []
    for table in ('jobs', 'shots', 'production_nodes', 'provider_calls'):
        values.append([tuple(r) for r in c.execute(f'SELECT * FROM {table} WHERE job_id=? ORDER BY rowid', (JOB,))])
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def check(c):
    job = c.execute('SELECT * FROM jobs WHERE job_id=?', (JOB,)).fetchone()
    if not job or job['state'] != 'FAILED' or job['production_worker_pid']:
        raise ValueError('incident is no longer an idle failed job')
    if provider_tasks.unresolved_task_ids(c, JOB):
        raise ValueError('remote submissions remain unresolved')
    if c.execute("SELECT 1 FROM production_nodes WHERE job_id=? AND (state='RUNNING' OR lease_id IS NOT NULL)", (JOB,)).fetchone():
        raise ValueError('an execution lease remains active')
    shot = c.execute('SELECT * FROM shots WHERE job_id=? AND idx=9', (JOB,)).fetchone()
    node = c.execute("SELECT * FROM production_nodes WHERE job_id=? AND node_id='master:9'", (JOB,)).fetchone()
    if not shot or shot['state'] != 'FAILED' or shot['cost_cents'] or shot['video_path'] or shot['video_qa']:
        raise ValueError('master 9 is not the original unpaid failure')
    if not node or node['state'] != 'FAILED' or node['provider_task_id'] or node['actual_cost_cents'] or json.loads(node['output_json'] or '{}'):
        raise ValueError('master 9 has execution evidence that must be retained')
    if c.execute('SELECT 1 FROM provider_calls WHERE job_id=? AND shot_idx=9', (JOB,)).fetchone():
        raise ValueError('master 9 already has a provider call')
    failure = c.execute("SELECT payload_json FROM production_node_events WHERE job_id=? AND node_id='master:9' AND event='fail' ORDER BY rowid DESC LIMIT 1", (JOB,)).fetchone()
    if not failure or 'product attention budget of 3 reference videos' not in json.loads(failure[0]).get('error', ''):
        raise ValueError('unexpected failure cause')
    spec, _ = production_adapters._frozen_spec(c, node)
    for board in (json.loads(job['storyboard']), spec['runtime_storyboard']):
        if production_config.generation_snapshot(board)['reference_video_budget'] != 4:
            raise ValueError('approved reference budget must remain four')
    accepted = {r[0] for r in c.execute("SELECT idx FROM shots WHERE job_id=? AND state='OK'", (JOB,))}
    if accepted != {1, 2, 3, 4, 7, 8}:
        raise ValueError('accepted masters changed; reassess before repair')
    if production_invariants.audit(c, JOB).blockers:
        raise ValueError('production invariants have unresolved blockers')
    return job


def verify_request(c, assets):
    """Exercise frozen adapter -> real S3 -> request boundary without a POST."""
    connections = []
    with asyncio.Runner() as runner, ExitStack() as stack:
        runner.get_loop()  # Windows creates a loopback socket before network denial.
        def connection():
            result = db.conn()
            connections.append(result)
            return result
        stack.enter_context(mock.patch.object(s3_shots, 'conn', side_effect=connection))
        stack.enter_context(mock.patch.object(s3_shots, 'ASSETS_DIR', assets))
        stack.enter_context(mock.patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        boundary = stack.enter_context(mock.patch.object(s3_shots, '_generate_seedance_video',
            new=mock.AsyncMock(side_effect=RuntimeError('VERIFIED_REQUEST_BOUNDARY'))))
        try:
            node = c.execute("SELECT * FROM production_nodes WHERE job_id=? AND node_id='master:9'", (JOB,)).fetchone()
            try:
                runner.run(production_adapters.run_master_node(c, node))
            except RuntimeError as exc:
                if 'VERIFIED_REQUEST_BOUNDARY' not in str(exc):
                    raise
            if boundary.await_count != 1:
                raise ValueError('master did not reach the request boundary exactly once')
            request = boundary.await_args.kwargs
            if request['generation_snapshot']['reference_video_budget'] != 4 or len(request['request_kwargs']['reference_videos']) != 4:
                raise ValueError('effective request did not retain four references')
            return {'request_budget': 4, 'reference_videos': 4, 'paid_submissions': 0}
        finally:
            for item in connections:
                item.close()


def repair(c, expected):
    with node_state_machine.atomic(c):
        if fingerprint(c) != expected:
            raise ValueError('task changed since rehearsal')
        job = check(c)
        retained = [tuple(r) for r in c.execute('SELECT * FROM shots WHERE job_id=? AND idx!=9 ORDER BY idx', (JOB,))]
        calls = [tuple(r) for r in c.execute('SELECT * FROM provider_calls WHERE job_id=? ORDER BY task_id', (JOB,))]
        node_state_machine.transition_node_in_transaction(c, JOB, 'master:9', 'retry',
            event_id=f'{JOB}:master:9:verified-reference-request-4-v2',
            payload={'reason': 'approved_reference_budget_verified_at_real_request_boundary', 'reference_video_budget': 4, 'no_prior_submission': True})
        c.execute("UPDATE production_nodes SET error_code=NULL,next_action=NULL WHERE job_id=? AND node_id='master:9'", (JOB,))
        c.execute("UPDATE shots SET state='PENDING',error=NULL WHERE job_id=? AND idx=9", (JOB,))
        result = recovery_plan.resume_ready_graph(c, JOB)
        if retained != [tuple(r) for r in c.execute('SELECT * FROM shots WHERE job_id=? AND idx!=9 ORDER BY idx', (JOB,))] or calls != [tuple(r) for r in c.execute('SELECT * FROM provider_calls WHERE job_id=? ORDER BY task_id', (JOB,))]:
            raise ValueError('retained production evidence changed')
        if production_invariants.audit(c, JOB).blockers:
            raise ValueError('repair did not restore consistent projections')
        return {**result, 'cost_cents': job['cost_cents'], 'provider_calls': len(calls)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    with tempfile.TemporaryDirectory() as td, closing(sqlite3.connect((root/'data/factory.db').as_uri()+'?mode=ro', uri=True)) as source:
        copy = Path(td)/'factory.db'
        with closing(sqlite3.connect(copy)) as rehearsal, mock.patch.object(db, 'database_path', return_value=copy):
            rehearsal.row_factory = sqlite3.Row
            source.backup(rehearsal)
            check(rehearsal)
            expected = fingerprint(rehearsal)
            proof = verify_request(rehearsal, Path(td)/'assets')
            source.backup(rehearsal)  # discard the deliberately intercepted attempt
            result = repair(rehearsal, expected)
            repaired_fingerprint = fingerprint(rehearsal)
            try:
                repair(rehearsal, expected)
            except ValueError:
                pass
            else:
                raise ValueError('repeated application unexpectedly succeeded')
            if fingerprint(rehearsal) != repaired_fingerprint:
                raise ValueError('repeated application changed the task')
            proof['repeat_application_rejected_without_changes'] = True
            service = ManagementService(rehearsal, root, worker_mode='external')
            view = next(j for j in service.videos()['jobs'] if j['job_id'] == JOB)
            preflight = view['preflight']
            proof['current_budget_cents'] = preflight['budget_cents']
            proof['projected_total_cost_cents'] = preflight['projected_total_cost_cents']
            proof['budget_confirmation_required'] = not preflight['budget_ok']
            with mock.patch.object(service, 'dispatch_production_queue', return_value={'dispatched_jobs': []}) as dispatch:
                # Simulated consent in the disposable copy only. Live repair
                # preserves the user's budget and stops before confirmation.
                started = service.start_video_job(JOB, confirmed=True, high_risk_confirmed=True,
                    approved_budget_cents=preflight['projected_total_cost_cents'])
            started_state = rehearsal.execute('SELECT state FROM jobs WHERE job_id=?', (JOB,)).fetchone()[0]
            if dispatch.call_count != 1 or started_state != 'PRODUCTION_QUEUED':
                raise ValueError('real start entry did not reach the queue')
            proof['start_entry_state'] = started_state
        if args.apply:
            backup = root/'data/backups'/('test26-reference-v2-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))/'factory.db'
            backup.parent.mkdir(parents=True)
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            with closing(sqlite3.connect(root/'data/factory.db')) as live:
                live.row_factory = sqlite3.Row
                result = repair(live, expected)
            result['backup'] = str(backup)
        print(json.dumps({**result, **proof, 'applied': args.apply}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
