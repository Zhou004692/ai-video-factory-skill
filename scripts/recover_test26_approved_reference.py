"""Rehearse Test26's frozen-model reference-limit failure; --apply backs up first.

No provider submission. Worker environment and CLI/graph/S3 run on a copy;
only a matching, unpaid historical failure can then return to budget confirmation.
"""
import argparse
from contextlib import ExitStack, closing, redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import io
from pathlib import Path
import sqlite3
import tempfile
from unittest import mock

# Match Worker startup: load the target installation environment BEFORE importing
# factory modules whose defaults are captured at import time.
_bootstrap = argparse.ArgumentParser(add_help=False)
_bootstrap.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
_environment, _ = _bootstrap.parse_known_args()
from dotenv import load_dotenv
load_dotenv(_environment.root.resolve() / '.env')
import cli
from factory.clients import seedance

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
    for idx in (5, 9):
        shot = c.execute('SELECT * FROM shots WHERE job_id=? AND idx=?', (JOB, idx)).fetchone()
        node = c.execute('SELECT * FROM production_nodes WHERE job_id=? AND node_id=?', (JOB, f'master:{idx}')).fetchone()
        if not shot or shot['state'] != 'FAILED' or shot['cost_cents'] or shot['video_path'] or shot['video_qa']:
            raise ValueError(f'master {idx} is not the original unpaid failure')
        if not node or node['state'] != 'FAILED' or node['provider_task_id'] or node['actual_cost_cents']:
            raise ValueError(f'master {idx} has execution evidence that must be retained')
        output = json.loads(node['output_json'] or '{}')
        if output and (set(output) != {'error'} or 'product attention budget of 3 reference videos' not in output['error']):
            raise ValueError(f'master {idx} has output beyond the known validation error')
        if c.execute('SELECT 1 FROM provider_calls WHERE job_id=? AND shot_idx=?', (JOB, idx)).fetchone():
            raise ValueError(f'master {idx} already has a provider call')
        failure = c.execute("SELECT payload_json FROM production_node_events WHERE job_id=? AND node_id=? AND event='fail' ORDER BY rowid DESC LIMIT 1", (JOB, f'master:{idx}')).fetchone()
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
    """Run the real CLI/graph/S3 with Worker .env; intercept the paid boundary."""
    connections = []
    c.execute("UPDATE jobs SET state='PRODUCTION_QUEUED' WHERE job_id=?", (JOB,))
    c.execute("UPDATE production_nodes SET state='READY' WHERE job_id=? AND node_id IN ('master:5','master:9')", (JOB,))
    c.commit()
    with ExitStack() as stack:
        def connection():
            result = db.conn()
            connections.append(result)
            return result
        stack.enter_context(mock.patch.object(s3_shots, 'conn', side_effect=connection))
        stack.enter_context(mock.patch.object(cli, 'conn', side_effect=connection))
        stack.enter_context(mock.patch.object(s3_shots, 'ASSETS_DIR', assets))
        stack.enter_context(mock.patch('httpx.Client.send', side_effect=AssertionError('network forbidden')))
        stack.enter_context(mock.patch('httpx.AsyncClient.send', side_effect=AssertionError('network forbidden')))
        boundary = stack.enter_context(mock.patch.object(s3_shots, '_generate_seedance_video',
            new=mock.AsyncMock(side_effect=RuntimeError('VERIFIED_REQUEST_BOUNDARY'))))
        try:
            with redirect_stdout(io.StringIO()):
                cli.produce_graph.callback(JOB, True)
            if boundary.await_count != 2:
                raise ValueError(f'masters did not reach the request boundary: {boundary.await_count}/2')
            for idx in (5, 9):
                output = c.execute('SELECT output_json FROM production_nodes WHERE job_id=? AND node_id=?',
                    (JOB, f'master:{idx}')).fetchone()[0]
                if 'VERIFIED_REQUEST_BOUNDARY' not in json.loads(output).get('error', ''):
                    raise ValueError(f'master {idx} did not reach the intercepted boundary')
            for call in boundary.await_args_list:
                request = call.kwargs
                snapshot = request['generation_snapshot']
                if snapshot['reference_video_budget'] != 4 or len(request['request_kwargs']['reference_videos']) != 4:
                    raise ValueError('effective request did not retain four references')
                if snapshot['model'] != 'doubao-seedance-2-5-260628':
                    raise ValueError('effective request model changed')
            return {'worker_default_model': seedance.DEFAULT_MODEL, 'verified_masters': [5, 9],
                'request_model': 'doubao-seedance-2-5-260628', 'request_budget': 4,
                'reference_videos_each': 4, 'paid_submissions': 0}
        finally:
            for item in connections:
                item.close()


def repair(c, expected):
    with node_state_machine.atomic(c):
        if fingerprint(c) != expected:
            raise ValueError('task changed since rehearsal')
        job = check(c)
        retained = [tuple(r) for r in c.execute('SELECT * FROM shots WHERE job_id=? AND idx NOT IN (5,9) ORDER BY idx', (JOB,))]
        calls = [tuple(r) for r in c.execute('SELECT * FROM provider_calls WHERE job_id=? ORDER BY task_id', (JOB,))]
        for idx in (5, 9):
            node_state_machine.transition_node_in_transaction(c, JOB, f'master:{idx}', 'retry',
                event_id=f'{JOB}:master:{idx}:verified-frozen-model-reference-4-v3',
                payload={'reason': 'frozen_model_verified_with_worker_environment', 'reference_video_budget': 4, 'no_prior_submission': True})
            c.execute("UPDATE production_nodes SET error_code=NULL,next_action=NULL,output_json='{}' WHERE job_id=? AND node_id=?", (JOB, f'master:{idx}'))
            c.execute("UPDATE shots SET state='PENDING',error=NULL WHERE job_id=? AND idx=?", (JOB, idx))
        result = recovery_plan.resume_ready_graph(c, JOB)
        if retained != [tuple(r) for r in c.execute('SELECT * FROM shots WHERE job_id=? AND idx NOT IN (5,9) ORDER BY idx', (JOB,))] or calls != [tuple(r) for r in c.execute('SELECT * FROM provider_calls WHERE job_id=? ORDER BY task_id', (JOB,))]:
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
                # Verify the existing approved budget is sufficient, in the copy only.
                started = service.start_video_job(JOB, confirmed=True, high_risk_confirmed=True,
                    approved_budget_cents=preflight['budget_cents'])
            started_state = rehearsal.execute('SELECT state FROM jobs WHERE job_id=?', (JOB,)).fetchone()[0]
            if dispatch.call_count != 1 or started_state != 'PRODUCTION_QUEUED':
                raise ValueError('real start entry did not reach the queue')
            proof['start_entry_state'] = started_state
        if args.apply:
            backup = root/'data/backups'/('test26-reference-model-v3-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))/'factory.db'
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
