"""Repair the confirmed Test26 review/attention incident without paid calls.

Default: rehearse on an in-memory SQLite backup. --apply: back up, then commit
the same guarded repair. The operator approved a reference budget of four.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from factory import (
    character_assets, node_state_machine, production_adapters, production_config,
    production_contract, production_graph, production_invariants,
    production_planner, provider_tasks, state_machine,
)
from factory.management_api import ManagementService
from factory.steps import s3_shots

ROOT = Path(__file__).resolve().parents[1]
JOB = 'J-TOPIC-24f362fa1c315927'


def repair(database):
    database.execute('BEGIN IMMEDIATE')
    try:
        job = database.execute('SELECT * FROM jobs WHERE job_id=?', (JOB,)).fetchone()
        assert job['state'] == 'FAILED' and job['production_worker_pid'] is None
        assert job['execution_engine'] == 'graph_v2'
        assert not provider_tasks.unresolved_task_ids(database, JOB)
        assert not database.execute(
            "SELECT 1 FROM production_nodes WHERE job_id=? AND (state='RUNNING' OR lease_id IS NOT NULL)",
            (JOB,),
        ).fetchone()
        shots_before = [tuple(r) for r in database.execute('SELECT * FROM shots WHERE job_id=? ORDER BY idx', (JOB,))]
        calls_before = [tuple(r) for r in database.execute('SELECT * FROM provider_calls WHERE job_id=? ORDER BY task_id', (JOB,))]
        costs_before = [tuple(r) for r in database.execute('SELECT node_id,actual_cost_cents FROM production_nodes WHERE job_id=? ORDER BY node_id', (JOB,))]
        states = {r['idx']: r['state'] for r in database.execute('SELECT idx,state FROM shots WHERE job_id=?', (JOB,))}
        assert {i for i, state in states.items() if state == 'OK'} == {1, 2, 3, 7, 8}
        assert all(states[i] == 'PENDING' for i in (4, 5, 6, 9, 10))
        assert database.execute(
            "SELECT 1 FROM events WHERE job_id=? AND step='master_candidate_review' AND message LIKE ?",
            (JOB, '母片 #4 已退回单条重做%'),
        ).fetchone(), 'no recorded user decision to regenerate #4'
        fourth = database.execute("SELECT * FROM production_nodes WHERE job_id=? AND node_id='master:4'", (JOB,)).fetchone()
        ninth = database.execute("SELECT * FROM production_nodes WHERE job_id=? AND node_id='master:9'", (JOB,)).fetchone()
        assert fourth['state'] == 'REVIEW_REQUIRED' and fourth['provider_task_id'] == 'cgt-20260908092005-pv8dx'
        assert ninth['state'] == 'FAILED' and not ninth['provider_task_id'] and ninth['actual_cost_cents'] == 0
        assert not json.loads(ninth['output_json'] or '{}')
        assert not database.execute('SELECT 1 FROM provider_calls WHERE job_id=? AND shot_idx=9', (JOB,)).fetchone()
        failure = database.execute(
            "SELECT payload_json FROM production_node_events WHERE job_id=? AND node_id='master:9' AND event='fail' ORDER BY rowid DESC LIMIT 1",
            (JOB,),
        ).fetchone()
        assert 'product attention budget of 3 reference videos' in json.loads(failure[0])['error']

        storyboard = json.loads(job['storyboard'])
        snapshot = storyboard['production_settings']['generation_snapshot']
        assert snapshot['reference_video_budget'] == 3
        snapshot['reference_video_budget'] = 4
        compilation = production_planner.compile_job_spec(
            database, JOB, ROOT, storyboard_override=storyboard, persist=False, target_version=2,
        )
        assert compilation.compilable, compilation.issues
        spec = compilation.spec
        graph = production_graph.compile_graph(spec)
        assert production_graph._existing_graph_is_equivalent(database, graph), 'executable graph changed'
        production_adapters.validate_frozen_execution_inputs(database, spec)

        # Rebuild the real #9 request locally with approved identity references
        # and the previous same-scene master. Never submit the request.
        unit = next(m for m in spec.runtime_storyboard['masters'] if m['idx'] == 9)
        characters = {c['id']: c for c in spec.runtime_storyboard['characters']}
        identities = {r['identity_key']: s3_shots.DialogueAnchor(video_url=r['video_url'], duration_sec=r['duration_sec'])
                      for r in database.execute("SELECT * FROM character_identity_anchors WHERE job_id=? AND state='READY' AND approval_state='APPROVED'", (JOB,))}
        previous = database.execute('SELECT video_url,duration_sec FROM shots WHERE job_id=? AND idx=1', (JOB,)).fetchone()
        cast = s3_shots._scene_cast_characters(unit, characters, unit['speaker'])
        request = s3_shots._dialogue_request_kwargs(
            production_config.attach_runtime_snapshot(unit, snapshot), None,
            character=characters[unit['speaker']], cast_characters=cast, identity_anchors=identities,
            scene_video_anchor=s3_shots.DialogueAnchor(video_url=previous['video_url'], duration_sec=previous['duration_sec']),
        )
        assert len(request['reference_videos']) == 4
        assert {r['identity_key'] for r in request['reference_manifest'] if r['identity_key']} == {
            character_assets.character_identity_key(characters[k]) for k in unit['cast']
        }

        production_graph.persist_graph(database, spec, commit=False)
        production_contract.persist_production_spec(database, spec, commit=False)
        database.execute('UPDATE jobs SET storyboard=? WHERE job_id=?', (json.dumps(storyboard, ensure_ascii=False), JOB))
        # Avoid ManagementService startup hooks: this transaction touches only
        # the explicitly verified incident rows, not other jobs or their files.
        service = object.__new__(ManagementService)
        service.database = database
        service._sync_graph_master_review_decision(JOB, 4, decision='regenerate_master', operation_id='test26-recorded-redo-repair')
        node_state_machine.transition_node_in_transaction(
            database, JOB, 'master:9', 'retry', event_id=f'{JOB}:master:9:reference-budget-4',
            payload={'reason': 'user_approved_reference_budget', 'previous_budget': 3, 'reference_video_budget': 4, 'no_prior_submission': True},
        )
        database.execute("UPDATE production_nodes SET error_code=NULL,next_action=NULL WHERE job_id=? AND node_id='master:9'", (JOB,))
        assert not production_invariants.audit(database, JOB).blockers
        assert shots_before == [tuple(r) for r in database.execute('SELECT * FROM shots WHERE job_id=? ORDER BY idx', (JOB,))]
        assert calls_before == [tuple(r) for r in database.execute('SELECT * FROM provider_calls WHERE job_id=? ORDER BY task_id', (JOB,))]
        assert costs_before == [tuple(r) for r in database.execute('SELECT node_id,actual_cost_cents FROM production_nodes WHERE job_id=? ORDER BY node_id', (JOB,))]
        state_machine.transition(
            database, JOB, 'resume_generation', event_id=f'{JOB}:remaining-masters-repaired',
            updates={'error': None, 'failure_code': None, 'retries': 0},
        )
        database.execute(
            "INSERT INTO events(job_id,step,level,message) VALUES(?,'remaining_master_repair','info',?)",
            (JOB, '按用户确认将参考视频上限从3调为4；同步第4条重做决定，恢复未提交的第9条；保留5条已完成母片与全部费用，未发起生成。'),
        )
        database.commit()
        return {'state': 'STORYBOARDED', 'ready_nodes': ['master:4', 'master:9'], 'reference_video_budget': 4,
                'preserved_masters': 5, 'cost_cents': job['cost_cents'], 'provider_calls': len(calls_before),
                'old_spec_hash': job['production_spec_hash'], 'spec_hash': spec.spec_hash, 'paid_submissions': 0}
    except Exception:
        database.rollback()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    source = sqlite3.connect((ROOT / 'data/factory.db').as_uri() + '?mode=ro', uri=True)
    rehearsal = sqlite3.connect(':memory:')
    rehearsal.row_factory = sqlite3.Row
    source.backup(rehearsal)
    result = repair(rehearsal)
    rehearsal.close()
    if args.apply:
        backup = ROOT / 'data/backups' / ('test26-remaining-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        backup.mkdir(parents=True)
        with sqlite3.connect(backup / 'factory.db') as target:
            source.backup(target)
        source.close()
        with sqlite3.connect(ROOT / 'data/factory.db') as live:
            live.row_factory = sqlite3.Row
            result = repair(live)
        result['backup'] = str(backup / 'factory.db')
    else:
        source.close()
    result['applied'] = args.apply
    print(json.dumps(result, ensure_ascii=False, indent=2))
