"""Known paid anchors reconcile; proven unsubmitted anchors remain retryable."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from factory import db, creative_ir, production_compiler, production_graph, production_adapters
from factory import network_recovery, provider_tasks
from factory.management_api import ManagementService


class AnchorNetworkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime_data = self.root / 'external-runtime-data'
        self.runtime_data.mkdir()
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row
        self.c.executescript(db.DDL)
        self.addCleanup(self.c.close)
        board = json.loads((Path(__file__).parent / 'fixtures/production_profiles/enhanced_multi_scene.json').read_text(encoding='utf-8'))
        board['production_settings'] = {'budget_cents':10000}
        self.spec = production_compiler.compile_ir(creative_ir.from_storyboard(board),
            job_id='J-ANCHOR', capabilities=production_compiler.ProviderCapabilities.seedance_2_5(),
            budget_cents=10000, runtime_storyboard=board).spec
        self.c.execute('INSERT INTO jobs(job_id,topic_id,title,state,storyboard,production_spec_json,production_spec_hash,production_spec_version,execution_engine) VALUES(?,?,?,?,?,?,?,?,?)',
            ('J-ANCHOR','topic','Anchor recovery','PROVIDER_ACTION_REQUIRED',json.dumps(self.spec.runtime_storyboard),json.dumps(self.spec.as_dict()),self.spec.spec_hash,2,'graph_v2'))
        production_graph.persist_graph(self.c, self.spec)
        production_adapters.materialize_frozen_execution_inputs(self.c, self.spec)
        self.node = self.c.execute("SELECT * FROM production_nodes WHERE node_type='scene_anchor'").fetchone()
        self.assertIsNotNone(self.node)
        self.key = json.loads(self.node['input_json'])['scene_key']
        self.c.execute("UPDATE production_nodes SET state='REVIEW_REQUIRED' WHERE node_type IN ('character_anchor','scene_anchor')")
        self.c.execute("UPDATE production_nodes SET state='ACTION_REQUIRED',provider_task_id='remote-anchor',actual_cost_cents=272 WHERE node_id=?", (self.node['node_id'],))
        self.c.execute("INSERT INTO scene_visual_anchors(job_id,anchor_key,description,master_indexes,state,generator,model,error) VALUES('J-ANCHOR',?,'Office','[1,2]','FAILED','seedance_2_5','doubao-seedance-2-5-260628','download unknown')", (self.key,))
        self.c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,cost_cents,usage_json) VALUES('remote-anchor','J-ANCHOR',-2001,'seedance_scene','generate',272,?)", (json.dumps({'status':'action_required'}),))
        self.c.commit()
        self.service = ManagementService(
            self.c,
            self.root,
            worker_mode='external',
            data_dir=self.runtime_data,
        )

    def anchor(self):
        return self.c.execute("SELECT * FROM scene_visual_anchors").fetchone()

    def completed(self):
        return {'status':'completed','video':b'paid-media','video_url':'https://cdn/video.mp4',
                'cost_cents':272,'usage':{'completion_tokens':38830}}

    async def frames(self, video, target, timestamps):
        target.mkdir(parents=True, exist_ok=True)
        frame = target / 'frame-001.jpg'
        frame.write_bytes(b'validated-preview')
        return [frame]

    def reconcile(self, result=None):
        with mock.patch('factory.management_api.seedance.inspect_task', new=mock.AsyncMock(return_value=result or self.completed())), \
             mock.patch('factory.clients.ffmpeg.extract_frames', side_effect=self.frames), \
             mock.patch('factory.clients.tos.upload', return_value='https://assets/anchor'):
            return self.service.reconcile_provider_task('J-ANCHOR','remote-anchor')

    def test_scene_download_reconciles_to_anchor_review_without_new_purchase(self):
        with mock.patch('factory.clients.seedance.text_to_video') as submit:
            result = self.reconcile()
        submit.assert_not_called()
        self.assertEqual(result['state'], 'ANCHOR_REVIEW_REQUIRED')
        self.assertEqual(self.anchor()['state'], 'READY')
        self.assertEqual(self.anchor()['approval_state'], 'PENDING_REVIEW')
        self.assertEqual(self.anchor()['cost_cents'], 272)
        self.assertEqual(self.c.execute('SELECT cost_cents FROM jobs').fetchone()[0],272)
        self.assertEqual(self.c.execute('SELECT state FROM production_nodes WHERE node_id=?',(self.node['node_id'],)).fetchone()[0],'REVIEW_REQUIRED')
        self.assertEqual(provider_tasks.unresolved_task_ids(self.c,'J-ANCHOR'),[])
        recovered_path = Path(self.anchor()['video_path'])
        self.assertEqual(recovered_path.read_bytes(),b'paid-media')
        self.assertTrue(recovered_path.is_relative_to(self.runtime_data.resolve()))
        self.assertFalse((self.root / 'data').exists())
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.c.execute('SELECT cost_cents FROM jobs').fetchone()[0],272)

    def test_failed_download_remains_reconcilable(self):
        result = self.reconcile({'status':'unknown'})
        self.assertEqual(result['state'],'PROVIDER_ACTION_REQUIRED')
        self.assertEqual(self.anchor()['state'],'FAILED')
        self.assertEqual(provider_tasks.unresolved_task_ids(self.c,'J-ANCHOR'),['remote-anchor'])

    def test_another_unresolved_task_keeps_job_paused(self):
        self.c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('other','J-ANCHOR',1,'seedance','generate',?)",(json.dumps({'status':'action_required'}),))
        self.c.commit()
        self.assertEqual(self.reconcile()['state'],'PROVIDER_ACTION_REQUIRED')
        self.assertEqual(provider_tasks.unresolved_task_ids(self.c,'J-ANCHOR'),['other'])
        self.assertEqual(self.anchor()['state'],'READY')

    def test_remote_failure_is_not_approved_or_automatically_reposted(self):
        self.assertEqual(self.reconcile({'status':'failed','cost_cents':272})['state'],'FAILED')
        self.assertEqual(self.anchor()['state'],'FAILED')
        self.assertEqual(self.c.execute('SELECT cost_cents FROM jobs').fetchone()[0],272)
        self.assertEqual(network_recovery.retry_anchor_ids(self.c,'J-ANCHOR'),[])

    def test_concurrent_termination_cannot_be_overwritten_by_download(self):
        def upload(*args,**kwargs):
            self.c.execute("UPDATE jobs SET state='REJECTED_PENDING_RECONCILIATION'")
            self.c.commit()
            return 'https://assets/anchor'
        with mock.patch('factory.management_api.seedance.inspect_task',new=mock.AsyncMock(return_value=self.completed())), \
             mock.patch('factory.clients.ffmpeg.extract_frames',side_effect=self.frames), \
             mock.patch('factory.clients.tos.upload',side_effect=upload):
            with self.assertRaisesRegex(ValueError,'concurrently'):
                self.service.reconcile_provider_task('J-ANCHOR','remote-anchor')
        self.assertEqual(self.anchor()['state'],'FAILED')
        self.assertEqual(self.c.execute('SELECT state FROM jobs').fetchone()[0],'REJECTED_PENDING_RECONCILIATION')
        self.assertEqual(provider_tasks.unresolved_task_ids(self.c,'J-ANCHOR'),['remote-anchor'])

    def test_changed_frozen_owner_is_rejected(self):
        self.c.execute("UPDATE production_nodes SET spec_hash='wrong' WHERE node_id=?",(self.node['node_id'],))
        self.c.commit()
        with self.assertRaises(production_adapters.FrozenSpecMismatchError):
            self.reconcile()

    def test_character_download_uses_same_recovery_contract(self):
        self.c.execute("UPDATE production_nodes SET state='REVIEW_REQUIRED',provider_task_id=NULL WHERE node_id=?",(self.node['node_id'],))
        node = self.c.execute("SELECT * FROM production_nodes WHERE node_type='character_anchor' LIMIT 1").fetchone()
        char_id = json.loads(node['input_json'])['character_id']
        self.c.execute("UPDATE production_nodes SET state='ACTION_REQUIRED',provider_task_id='remote-anchor',actual_cost_cents=272 WHERE node_id=?",(node['node_id'],))
        self.c.execute("UPDATE provider_calls SET shot_idx=-1001,provider='seedance_identity'")
        self.c.execute("INSERT INTO character_identity_anchors(job_id,identity_key,character_id,display_name,source_type,state,generator) VALUES('J-ANCHOR','identity',?,'Actor','seedance_identity_anchor','FAILED','seedance_2_5')",(char_id,))
        self.c.commit()
        self.assertEqual(self.reconcile()['state'],'ANCHOR_REVIEW_REQUIRED')
        row = self.c.execute('SELECT * FROM character_identity_anchors').fetchone()
        self.assertEqual(row['state'],'READY')
        self.assertEqual(row['approval_state'],'PENDING_REVIEW')
        self.assertEqual(row['duration_sec'],4)
        self.assertEqual(row['cost_cents'],272)

    def test_stopped_anchor_recovers_in_another_process_without_paid_submit(self):
        for kind in ('scene', 'character'):
            with self.subTest(kind=kind):
                if kind == 'character':
                    self.c.execute("UPDATE production_nodes SET state='REVIEW_REQUIRED',provider_task_id=NULL WHERE node_id=?", (self.node['node_id'],))
                    node = self.c.execute("SELECT * FROM production_nodes WHERE node_type='character_anchor' LIMIT 1").fetchone()
                    char_id = json.loads(node['input_json'])['character_id']
                    self.c.execute("UPDATE provider_calls SET shot_idx=-1001,provider='seedance_identity'")
                    self.c.execute("INSERT INTO character_identity_anchors(job_id,identity_key,character_id,display_name,source_type,state,generator) VALUES('J-ANCHOR','identity',?,'Actor','seedance_identity_anchor','FAILED','seedance_2_5')", (char_id,))
                else:
                    node = self.node
                self.c.execute("UPDATE production_nodes SET state='RUNNING',lease_id='dead-worker-lease',heartbeat_at='2099-01-01T00:00:00+00:00',provider_task_id='remote-anchor' WHERE node_id=?", (node['node_id'],))
                self.c.execute("UPDATE jobs SET state='FAILED',failure_code='internal_error',production_worker_pid=NULL")
                self.c.commit()
                persisted = self.root / f'{kind}.db'
                with closing(sqlite3.connect(persisted)) as target:
                    self.c.backup(target)
                report = self.runtime_data / f'{kind}-recovery.json'
                child = subprocess.run(
                    [sys.executable, str(Path(__file__).parent / 'process_recovery_child.py'),
                     'anchor', str(persisted), str(self.runtime_data), 'J-ANCHOR',
                     'remote-anchor', str(report)],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])},
                    capture_output=True, text=True, timeout=45,
                )
                self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
                evidence = json.loads(report.read_text(encoding='utf-8'))
                self.assertNotEqual(evidence['pid'], os.getpid())
                self.assertEqual(evidence['result']['state'], 'ANCHOR_REVIEW_REQUIRED')
                with closing(sqlite3.connect(persisted)) as observed:
                    observed.row_factory = sqlite3.Row
                    table = ('scene_visual_anchors' if kind == 'scene'
                             else 'character_identity_anchors')
                    anchor = observed.execute(f'SELECT * FROM {table}').fetchone()
                    self.assertEqual(anchor['state'], 'READY')
                    self.assertEqual(anchor['approval_state'], 'PENDING_REVIEW')
                    self.assertEqual(anchor['cost_cents'], 272)
                    self.assertEqual(Path(anchor['video_path']).read_bytes(), b'paid-media')
                    self.assertEqual(observed.execute('SELECT cost_cents FROM jobs').fetchone()[0], 272)
                    self.assertEqual(observed.execute('SELECT state FROM production_nodes WHERE node_id=?', (node['node_id'],)).fetchone()[0], 'REVIEW_REQUIRED')
                    self.assertEqual(provider_tasks.unresolved_task_ids(observed, 'J-ANCHOR'), [])
                if kind == 'scene':
                    # Reset only the in-memory source; the second child starts
                    # from a separate frozen database snapshot.
                    self.c.execute("UPDATE production_nodes SET state='REVIEW_REQUIRED',provider_task_id=NULL,lease_id=NULL,heartbeat_at=NULL WHERE node_id=?", (self.node['node_id'],))
                    self.c.commit()

    def test_corrupt_media_never_becomes_reviewable(self):
        with mock.patch('factory.management_api.seedance.inspect_task',new=mock.AsyncMock(return_value=self.completed())), \
             mock.patch('factory.clients.ffmpeg.extract_frames',new=mock.AsyncMock(side_effect=RuntimeError('invalid media'))):
            with self.assertRaisesRegex(RuntimeError,'invalid media'):
                self.service.reconcile_provider_task('J-ANCHOR','remote-anchor')
        self.assertEqual(self.anchor()['state'],'FAILED')
        self.assertEqual(provider_tasks.unresolved_task_ids(self.c,'J-ANCHOR'),['remote-anchor'])

    def test_database_failure_preserves_unresolved_ledger_and_old_asset(self):
        self.c.execute("CREATE TRIGGER fail_anchor BEFORE UPDATE OF state ON scene_visual_anchors BEGIN SELECT RAISE(ABORT,'anchor write failed'); END")
        self.c.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError,'anchor write failed'):
            self.reconcile()
        self.assertEqual(provider_tasks.unresolved_task_ids(self.c,'J-ANCHOR'),['remote-anchor'])
        self.assertEqual(self.c.execute('SELECT cost_cents FROM jobs').fetchone()[0],0)

    def unsubmitted(self):
        self.c.execute('DELETE FROM provider_calls')
        self.c.execute("UPDATE jobs SET state='FAILED'")
        self.c.execute("UPDATE production_nodes SET state='FAILED',provider_task_id=NULL,actual_cost_cents=0,error_code='SeedanceSubmissionUnknownError',output_json=? WHERE node_id=?",
            (json.dumps({'error':'Seedance 提交前连接失败；本次错误归类为 transient_network，可按重试策略处理。'}),self.node['node_id']))
        self.c.commit()

    def test_pre_submit_anchor_has_real_ui_retry_action_and_queue(self):
        self.unsubmitted()
        job = self.service.videos()['jobs'][0]
        self.assertEqual(job['next_action']['id'],'retry_network_failures')
        self.assertEqual(job['network_retry_anchor_ids'],[self.node['node_id']])
        with mock.patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}):
            self.service.retry_network_failures('J-ANCHOR',confirmed=True)
        self.assertEqual(self.c.execute('SELECT state FROM jobs').fetchone()[0],'PRODUCTION_QUEUED')
        self.assertEqual(self.c.execute('SELECT state FROM production_nodes WHERE node_id=?',(self.node['node_id'],)).fetchone()[0],'READY')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0],0)

    def test_ambiguous_submit_and_paid_anchors_cannot_retry(self):
        self.unsubmitted()
        for update in ["output_json='{}'", "provider_task_id='known-task'", 'actual_cost_cents=272']:
            with self.subTest(update=update):
                self.c.execute('SAVEPOINT variant')
                self.c.execute('UPDATE production_nodes SET '+update+' WHERE node_id=?',(self.node['node_id'],))
                self.assertEqual(network_recovery.retry_anchor_ids(self.c,'J-ANCHOR'),[])
                self.c.execute('ROLLBACK TO variant')
                self.c.execute('RELEASE variant')

    def test_unknown_sibling_blocks_pre_submit_retry(self):
        self.unsubmitted()
        self.c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('unknown','J-ANCHOR',1,'seedance','generate',?)",(json.dumps({'status':'unknown'}),))
        self.c.commit()
        self.assertEqual(network_recovery.retry_anchor_ids(self.c,'J-ANCHOR'),[])


if __name__ == '__main__':
    unittest.main()
