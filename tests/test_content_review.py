"""Offline controlled human authority and local content boundaries."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import asyncio
from copy import deepcopy
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib import request, error
import unittest
from unittest.mock import patch, AsyncMock

from factory import db, visual_contract, visual_evidence, content_review, recovery_plan
from factory.management_api import ManagementService, REVIEW_CHECKS

REQS = [{"id": "hand", "description": "Raised hand", "priority": "critical"}]


class ContentReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = sqlite3.connect(':memory:', check_same_thread=False)
        self.database.row_factory = sqlite3.Row
        self.database.executescript(db.DDL)
        self.service = ManagementService(self.database, self.root)
        self.base = self.root/'data'/'assets'/'J'
        (self.base/'review').mkdir(parents=True)
        self.active = self.base/'01.mp4'; self.active.write_bytes(b'prior')
        self.candidate = self.base/'review'/'01.mp4'; self.candidate.write_bytes(b'candidate')
        self.bgm = self.base/'bgm.mp3'; self.bgm.write_bytes(b'music')
        self.duration_patch = patch.object(content_review, 'probe_duration', return_value=4)
        self.duration_patch.start()
        self.sb = {'production_mode': 'native_master_local_director',
            'visual_requirements': REQS, 'masters': [{'idx': 1, 'duration': 4, 'type': 'broll', 'visual_requirements': REQS}],
            'shots': [{'idx': 1, 'video_master_idx': 1, 'duration': 4}]}
        self.qa = {'status': 'manual_review_required', 'candidate_path': str(self.candidate),
                   'diagnostic': {'visual_evidence': {'provenance': 'human'}}}
        self.database.execute("INSERT INTO jobs(job_id,topic_id,title,state,storyboard) VALUES('J','T','Test','FAILED',?)", (json.dumps(self.sb),))
        self.database.execute("INSERT INTO shots(job_id,idx,state,video_path,video_qa) VALUES('J',1,'FAILED',?,?)", (str(self.active), json.dumps(self.qa)))
        self.database.execute('UPDATE jobs SET bgm_path=?', (str(self.bgm),))
        self.database.commit()

    def tearDown(self):
        self.duration_patch.stop(); self.database.close(); self.temp.cleanup()

    def payload(self, source='candidate', status='satisfied'):
        path = self.candidate if source == 'candidate' else self.active
        return {'source': source, 'expected_asset_sha256': visual_evidence.asset_fingerprint(path),
            'expected_requirement_set_hash': visual_contract.requirement_set_hash(REQS),
            'items': [{'id': 'hand', 'status': status, 'start_sec': 1, 'end_sec': 2,
                       'observed_description': 'Hand rises', 'reason': 'Operator waiver'}]}

    def test_explicit_controlled_review_entry_exists(self):
        self.assertTrue(callable(getattr(self.service, 'record_content_review', None)),
                        'Explicit controlled review entry is required')

    def test_generic_candidate_accept_cannot_bypass_content(self):
        with patch('factory.management_api.s6_compose.run', new=AsyncMock()):
            with self.assertRaisesRegex(ValueError, 'content'):
                self.service.review_master_candidate('J', 1, decision='accept_candidate')
        self.assertEqual(self.candidate.read_bytes(), b'candidate')
        self.assertEqual(self.database.execute("SELECT state FROM shots").fetchone()[0], 'FAILED')

    def test_ready_publish_early_return_checks_missing_report(self):
        final = self.base/'final.mp4'; final.write_bytes(b'final')
        self.database.execute("UPDATE jobs SET state='READY_TO_PUBLISH',final_path=?", (str(final),)); self.database.commit()
        with self.assertRaisesRegex(ValueError, 'content'):
            self.service.submit_for_publish('J')

    def test_local_content_pause_has_review_entry(self):
        from factory import recovery_plan
        self.database.execute("UPDATE jobs SET execution_engine='graph_v2',state='PRODUCTION_REVIEW_REQUIRED'")
        self.database.execute("UPDATE shots SET state='OK'")
        self.database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,error_code,spec_hash,idempotency_key) VALUES('J','master:1','master','SUCCEEDED',NULL,'frozen','master-key')")
        self.database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,error_code,spec_hash,idempotency_key) VALUES('J','local_compose','local_compose','REVIEW_REQUIRED','content_review_required','frozen','local-key')")
        self.database.commit()
        plan = recovery_plan.resolve(self.database, 'J')
        self.assertIsNotNone(plan)
        self.assertEqual(plan['next_action']['id'], 'review_visual_content')

    def test_controlled_manual_is_durable_idempotent_and_not_candidate_accept(self):
        first = self.service.record_content_review('J', 1, self.payload())
        duplicate = self.service.record_content_review('J', 1, self.payload())
        self.assertFalse(first['already_recorded']); self.assertTrue(duplicate['already_recorded'])
        restarted = ManagementService(self.database, self.root)
        detail = restarted.content_review_detail('J', 1)
        self.assertEqual(detail['human_review'], first['review'])
        self.assertEqual(detail['human_review']['provenance'], 'human')
        row = self.database.execute('SELECT state,video_qa FROM shots').fetchone()
        qa = json.loads(row['video_qa'])
        self.assertEqual(row['state'], 'FAILED')
        self.assertEqual(qa['status'], 'manual_review_required')
        self.assertEqual(qa['diagnostic'], self.qa['diagnostic'])
        self.assertEqual(len(qa['content_reviews']), 1)

    def test_manual_pass_and_waiver_gate_accept_candidate(self):
        for status in ['satisfied', 'not_applicable']:
            self.service.record_content_review('J', 1, self.payload(status=status))
            content_review.require_selected(self.database, self.root/'data'/'assets', 'J', 1, 'candidate')
        saved = self.service.content_review_detail('J', 1)['human_review']['items'][0]
        self.assertEqual(saved['status'], 'not_applicable')
        self.assertEqual(saved['reason'], 'Operator waiver')

    def test_explicit_negative_overrides_valid_machine_proof(self):
        from test_visual_evidence import observation
        sha = visual_evidence.asset_fingerprint(self.candidate)
        self.qa['result'] = {'visual_evidence': visual_evidence.bind_observations(REQS, observation(), asset_sha256=sha, duration_sec=4)}
        self.database.execute('UPDATE shots SET video_qa=?', (json.dumps(self.qa),)); self.database.commit()
        content_review.require_selected(self.database, self.root/'data'/'assets', 'J', 1, 'candidate')
        for status in ['conflicting', 'insufficient_evidence']:
            self.service.record_content_review('J', 1, self.payload(status=status))
            with self.assertRaisesRegex(ValueError, 'content'):
                self.service.review_master_candidate('J', 1, decision='accept_candidate')

    def test_candidate_proof_does_not_authorize_prior(self):
        self.service.record_content_review('J', 1, self.payload())
        with self.assertRaisesRegex(ValueError, 'content'):
            self.service.review_master_candidate('J', 1, decision='keep_prior')

    def test_stale_hash_and_changed_definitions_reject_confirmation(self):
        stale = self.payload(); self.candidate.write_bytes(b'replaced')
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.service.record_content_review('J', 1, stale)
        current = self.payload(); self.sb['masters'][0]['visual_requirements'] = [{**REQS[0], 'description': 'Different hand'}]
        self.database.execute('UPDATE jobs SET storyboard=?', (json.dumps(self.sb),)); self.database.commit()
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.service.record_content_review('J', 1, current)

    def test_invalid_intervals_ids_and_waivers_are_rejected(self):
        cases = [{'start_sec': float('nan')}, {'end_sec': float('inf')}, {'end_sec': 5},
                 {'start_sec': -1}, {'start_sec': True}, {'observed_description': ''},
                 {'id': 'unknown'}, {'status': 'not_applicable', 'reason': ''}]
        for changes in cases:
            with self.subTest(changes=changes):
                payload = self.payload(); payload['items'][0].update(changes)
                with self.assertRaisesRegex(ValueError, 'content'):
                    self.service.record_content_review('J', 1, payload)
        payload = self.payload(); payload['items'].append(dict(payload['items'][0]))
        with self.assertRaises(ValueError): self.service.record_content_review('J', 1, payload)

    def test_arbitrary_source_paths_are_not_accepted(self):
        with self.assertRaises(ValueError): self.service.content_review_source_file('J', 1, '../secret')
        self.qa['candidate_path'] = str(self.root/'outside.mp4')
        self.database.execute('UPDATE shots SET video_qa=?', (json.dumps(self.qa),)); self.database.commit()
        with self.assertRaises(ValueError): self.service.content_review_detail('J', 1)

    def test_root_deletion_cannot_make_master_contract_legacy(self):
        self.sb['visual_requirements'] = []
        self.database.execute('UPDATE jobs SET storyboard=?', (json.dumps(self.sb),)); self.database.commit()
        with self.assertRaisesRegex(ValueError, 'content contract'):
            self.service.review_master_candidate('J', 1, decision='accept_candidate')

    def graph_pause(self):
        self.test_local_content_pause_has_review_entry()
        self.database.execute("UPDATE shots SET video_qa=?", (json.dumps({'status': 'passed'}),)); self.database.commit()

    def test_resume_after_review_only_readies_local_and_duplicate_is_idempotent(self):
        self.graph_pause()
        with self.assertRaisesRegex(ValueError, 'content'):
            self.service.resume_visual_content('J')
        self.service.record_content_review('J', 1, self.payload('active'))
        first = self.service.resume_visual_content('J'); second = self.service.resume_visual_content('J')
        self.assertEqual(first['state'], 'PRODUCTION_QUEUED'); self.assertTrue(second['already_queued'])
        nodes = {r['node_id']: dict(r) for r in self.database.execute('SELECT * FROM production_nodes')}
        self.assertEqual(nodes['master:1']['state'], 'SUCCEEDED'); self.assertEqual(nodes['local_compose']['state'], 'READY')
        self.assertEqual(nodes['master:1']['spec_hash'], 'frozen')
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM production_node_events WHERE event='resume_visual_content'").fetchone()[0], 1)
        self.assertEqual(self.database.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0], 0)
        self.assertEqual(self.database.execute('SELECT bgm_path FROM jobs').fetchone()[0], str(self.bgm))

    def test_cropped_action_cannot_resume_even_after_human_satisfied(self):
        self.graph_pause(); self.service.record_content_review('J', 1, self.payload('active'))
        self.sb['shots'][0].update(video_start=2.1, duration=1)
        self.database.execute('UPDATE jobs SET storyboard=?', (json.dumps(self.sb),)); self.database.commit()
        with self.assertRaisesRegex(ValueError, 'locally recut'):
            self.service.resume_visual_content('J')
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='local_compose'").fetchone()[0], 'REVIEW_REQUIRED')

    def final_report(self):
        self.service.record_content_review('J', 1, self.payload('active'))
        final = self.base/'final.mp4'; final.write_bytes(b'final')
        self.database.execute("UPDATE shots SET state='OK'")
        self.database.execute("UPDATE jobs SET state='COMPOSED',final_path=?", (str(final),)); self.database.commit()
        rows = self.database.execute('SELECT * FROM shots').fetchall()
        report = content_review.retention(self.sb, rows, {int(r['idx']):4 for r in rows})
        report.update(final_sha256=visual_evidence.asset_fingerprint(final),
            contract_hash=content_review.contract_hash(self.sb), edit_plan_fingerprint=content_review.edit_plan_fingerprint(self.sb),
            source_hashes=content_review.source_hashes(self.sb, rows, self.bgm))
        (self.base/'content_retention.json').write_text(json.dumps(report), encoding='utf-8')
        return final, report

    def test_final_approval_and_publish_are_bound_to_sources_plan_and_final(self):
        final, report = self.final_report()
        content_review.require_final(self.database, self.root/'data'/'assets', 'J')
        for target in [final, self.active, self.bgm]:
            original = target.read_bytes(); target.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'content'):
                self.service.review_video('J', decision='approved', checks={c: True for c in REVIEW_CHECKS})
            self.database.execute("UPDATE jobs SET state='READY_TO_PUBLISH'"); self.database.commit()
            with self.assertRaisesRegex(ValueError, 'content'): self.service.submit_for_publish('J')
            with self.assertRaisesRegex(ValueError, 'content'): self.service.publish_to_douyin('J', {'confirmed': True, 'text': 'x'})
            with self.assertRaisesRegex(ValueError, 'content'): self.service.register_publication({'job_id':'J','platform':'douyin','item_id':'x'})
            target.write_bytes(original); self.database.execute("UPDATE jobs SET state='COMPOSED'"); self.database.commit()
        for changes in [{'audio_start': .2}, {'transition_type': 'fade', 'transition_duration_sec': .3}, {'video_start': .1}]:
            edit = dict(self.sb['shots'][0]); self.sb['shots'][0].update(changes)
            self.database.execute('UPDATE jobs SET storyboard=?', (json.dumps(self.sb),)); self.database.commit()
            with self.assertRaisesRegex(ValueError, 'content'): content_review.require_final(self.database, self.root/'data'/'assets', 'J')
            self.sb['shots'][0] = edit
        self.database.execute('UPDATE jobs SET storyboard=?', (json.dumps(self.sb),)); self.database.commit()
        report['passed'] = 'true'; (self.base/'content_retention.json').write_text(json.dumps(report), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'content'): self.service.submit_for_publish('J')

    def test_changed_human_waiver_invalidates_final_and_exposes_local_recovery(self):
        self.final_report()
        self.service.record_content_review('J', 1, self.payload('active', 'not_applicable'))
        self.assertEqual(self.database.execute('SELECT state FROM jobs').fetchone()[0], 'BGM_OK')
        with self.assertRaisesRegex(ValueError, 'content'): content_review.require_final(self.database, self.root/'data'/'assets', 'J')
        self.assertEqual(self.service.videos()['jobs'][0]['next_action']['id'], 'review_visual_content')

    def test_reject_final_can_record_notes_without_content_report(self):
        final = self.base/'final.mp4'; final.write_bytes(b'final')
        self.database.execute("UPDATE jobs SET state='COMPOSED',final_path=?", (str(final),)); self.database.commit()
        result = self.service.review_video('J', decision='rejected', notes='Action missing')
        self.assertEqual(self.database.execute('SELECT notes FROM review_events').fetchone()[0], 'Action missing')

    def test_http_detail_save_preview_and_resume_routes_call_real_service(self):
        from management_server import ConsoleHandler
        class Handler(ConsoleHandler): pass
        Handler.service = self.service
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/videos/J/masters/1'
        try:
            with request.urlopen(url+'/content-review?source=candidate') as response:
                self.assertEqual(json.load(response)['source'], 'candidate')
            body = json.dumps(self.payload()).encode()
            with request.urlopen(request.Request(url+'/content-review', body, {'Content-Type':'application/json'}, method='POST')) as response:
                self.assertEqual(json.load(response)['review']['authority'], content_review.AUTHORITY)
            with request.urlopen(url+'/content-preview?source=active') as response: self.assertEqual(response.read(), b'prior')
            self.graph_pause(); self.service.record_content_review('J', 1, self.payload('active'))
            with request.urlopen(request.Request(url.rsplit('/masters/',1)[0]+'/resume-visual-content', b'{}', {'Content-Type':'application/json'}, method='POST')) as response:
                self.assertEqual(json.load(response)['state'], 'PRODUCTION_QUEUED')
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_real_accept_keeps_controlled_record_and_committed_repeat_checks_active(self):
        self.service.record_content_review('J', 1, self.payload())
        with patch('factory.management_api.s6_compose.run', new=AsyncMock()) as compose, \
             patch('factory.management_api.s5_bgm.run', new=AsyncMock(side_effect=AssertionError('paid music forbidden'))) as music:
            self.service.review_master_candidate('J', 1, decision='accept_candidate')
            self.assertEqual(self.active.read_bytes(), b'candidate')
            self.assertFalse(self.candidate.exists())
            result = self.service.review_master_candidate('J', 1, decision='accept_candidate')
            self.assertTrue(result['already_committed']); self.assertEqual(compose.await_count, 1); music.assert_not_awaited()
            self.assertIsNotNone(self.service.content_review_detail('J', 1, 'active')['human_review'])
            self.active.write_bytes(b'changed-active')
            with self.assertRaisesRegex(ValueError, 'content'):
                self.service.review_master_candidate('J', 1, decision='accept_candidate')

    def test_successful_legacy_resume_preserves_music_and_provider_ledger(self):
        self.database.execute("UPDATE shots SET state='OK'")
        self.database.execute("UPDATE jobs SET state='BGM_OK',failure_code='content_review_required',cost_cents=123")
        self.database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,cost_cents) VALUES('paid-original','J',1,'seedance',123)")
        self.database.commit(); ledger = [tuple(r) for r in self.database.execute('SELECT * FROM provider_calls')]
        self.service.record_content_review('J', 1, self.payload('active'))
        async def local_render(_job):
            self.final_report()
            self.database.execute("UPDATE jobs SET state='BGM_OK'"); self.database.commit()
        with patch('factory.management_api.s6_compose.run', new=AsyncMock(side_effect=local_render)) as compose, \
             patch('factory.management_api.s5_bgm.run', new=AsyncMock(side_effect=AssertionError('paid music forbidden'))) as music:
            self.service.resume_visual_content('J')
            self.service.resume_visual_content('J')
            self.assertEqual(compose.await_count, 1); music.assert_not_awaited()
        self.assertEqual([tuple(r) for r in self.database.execute('SELECT * FROM provider_calls')], ledger)
        self.assertEqual(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0], 123)

    def test_compose_consumes_only_controlled_current_human_record(self):
        from factory.steps import s6_compose
        self.service.record_content_review('J', 1, self.payload('active'))
        rows = self.database.execute('SELECT * FROM shots').fetchall()
        with patch.object(s6_compose.ffmpeg, 'probe_duration', new=AsyncMock(return_value=4)):
            report = asyncio.run(s6_compose._content_retention(self.sb, rows))
            self.assertTrue(report['passed'])
            qa = json.loads(rows[0]['video_qa'])
            qa['diagnostic'] = {'visual_evidence': qa['content_reviews'][0]}; qa.pop('content_reviews')
            row = dict(rows[0]); row['video_qa'] = json.dumps(qa)
            with self.assertRaises(visual_evidence.ContentReviewRequired): asyncio.run(s6_compose._content_retention(self.sb, [row]))

    def test_broll_and_audio_only_sources_are_bound_to_final(self):
        broll = self.base/'02.mp4'; broll.write_bytes(b'background')
        self.sb['masters'].append({'idx':2, 'duration':4, 'type':'broll'})
        self.sb['shots'].append({'idx':2, 'video_master_idx':2, 'audio_master_idx':1, 'duration':2})
        self.database.execute('UPDATE jobs SET storyboard=?', (json.dumps(self.sb),))
        self.database.execute("INSERT INTO shots(job_id,idx,state,video_path) VALUES('J',2,'OK',?)", (str(broll),)); self.database.commit()
        self.final_report(); content_review.require_final(self.database, self.root/'data'/'assets', 'J')
        broll.write_bytes(b'other-background')
        with self.assertRaisesRegex(ValueError, 'content'): self.service.submit_for_publish('J')

    def test_s7_actual_manual_registration_boundary_rejects_missing_report(self):
        from factory.steps import s7_publish
        final = self.base/'final.mp4'; final.write_bytes(b'final')
        self.database.execute("UPDATE jobs SET state='READY_TO_PUBLISH',final_path=?", (str(final),)); self.database.commit()
        with patch.object(s7_publish, 'ASSETS_DIR', self.root/'data'/'assets'):
            with self.assertRaisesRegex(ValueError, 'content'): asyncio.run(s7_publish._run('J', self.database))
        self.assertIsNone(self.database.execute('SELECT publish_url FROM jobs').fetchone()[0])

    def test_ui_dispatch_executes_new_action_without_browser_evidence(self):
        source = (Path(__file__).resolve().parents[1]/'scripts'/'management_ui'/'app.js').read_text(encoding='utf-8')
        dispatcher = source.split('function runVideoNextAction',1)[1].split('\n',1)[0]
        script = "let observed=null;function openVisualContentReview(job){observed=job.job_id}function toast(x){throw Error(x)}\nfunction runVideoNextAction"+dispatcher+"\nrunVideoNextAction({job_id:'J',next_action:{id:'review_visual_content'}});if(observed!=='J')throw Error('dispatch missing')"
        result = subprocess.run(['node','-e',script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recomposed_graph_final_can_be_approved_as_new_revision(self):
        from factory import node_state_machine
        final, _ = self.final_report()
        self.database.execute("UPDATE jobs SET execution_engine='graph_v2'")
        for node_id, node_type, state in [('master:1','master','SUCCEEDED'),('local_compose','local_compose','SUCCEEDED'),('final_review','final_review','REVIEW_REQUIRED')]:
            self.database.execute('INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,idempotency_key) VALUES(?,?,?,?,?,?)', ('J',node_id,node_type,state,'frozen',node_id))
        self.database.execute("INSERT INTO production_edges(job_id,from_node_id,to_node_id,edge_type) VALUES('J','local_compose','final_review','requires')"); self.database.commit()
        first = self.service.review_video('J', decision='approved', checks={c:True for c in REVIEW_CHECKS})
        self.assertTrue(first['publish_unlocked'])
        self.service.record_content_review('J', 1, self.payload('active','not_applicable'))
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='final_review'").fetchone()[0], 'PENDING')
        self.service.resume_visual_content('J')
        for node_id, events in [('local_compose',['start','succeed']),('final_review',['start','require_review'])]:
            for event in events: node_state_machine.transition_node(self.database, 'J', node_id, event, event_id=f'second:{node_id}:{event}')
        final.write_bytes(b'recomposed-final')
        rows = self.database.execute('SELECT * FROM shots').fetchall()
        report = content_review.retention(self.sb, rows, {1:4})
        report.update(final_sha256=visual_evidence.asset_fingerprint(final),contract_hash=content_review.contract_hash(self.sb),
            edit_plan_fingerprint=content_review.edit_plan_fingerprint(self.sb),source_hashes=content_review.source_hashes(self.sb, rows, self.bgm))
        (self.base/'content_retention.json').write_text(json.dumps(report),encoding='utf-8')
        self.database.execute("UPDATE jobs SET state='COMPOSED'"); self.database.commit()
        second = self.service.review_video('J', decision='approved', checks={c:True for c in REVIEW_CHECKS})
        self.assertTrue(second['publish_unlocked'])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM production_node_events WHERE node_id='final_review' AND event='approve'").fetchone()[0], 2)

    def test_bulk_projection_uses_content_metadata_without_asset_hashes(self):
        self.service.record_content_review('J', 1, self.payload())
        with patch.object(visual_evidence, 'asset_fingerprint', side_effect=AssertionError('bulk hash forbidden')):
            job = self.service.videos()['jobs'][0]
        self.assertTrue(job['has_visual_requirements'])
        self.assertEqual(len(job['shots'][0]['content_review_summary']), 1)

    def test_selected_duration_comes_from_real_local_media(self):
        result = subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=32x32:r=10',
            '-t','1','-c:v','libx264','-pix_fmt','yuv420p','-y',str(self.candidate)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.duration_patch.stop()
        try:
            detail = self.service.content_review_detail('J', 1)
            self.assertAlmostEqual(detail['duration_sec'], 1., places=1)
            payload = self.payload()
            with self.assertRaisesRegex(ValueError, 'actual duration'):
                self.service.record_content_review('J', 1, payload)
        finally: self.duration_patch.start()

    def test_adapter_override_without_asr_change_persists_actual_local_plan(self):
        from factory import production_adapters
        from factory.steps import s6_compose
        self.service.record_content_review('J', 1, self.payload('active'))
        frozen = deepcopy(self.sb); frozen.pop('shots')
        frozen['production_settings'] = {'asr_enabled': False, 'budget_cents': 1000}
        live = deepcopy(frozen); live['production_settings']['budget_cents'] = 9999
        spec = {'spec_hash':'frozen-contract', 'runtime_storyboard':frozen}
        encoded_spec = json.dumps(spec)
        self.database.execute("UPDATE jobs SET execution_engine='graph_v2',state='PRODUCTION_RUNNING',storyboard=?,production_spec_json=?,production_spec_hash='frozen-contract',production_spec_version=2", (json.dumps(live), encoded_spec))
        self.database.execute("UPDATE shots SET state='OK'")
        self.database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,idempotency_key) VALUES('J','local_compose','local_compose','RUNNING','frozen-contract','local-key')")
        self.database.commit()
        node = self.database.execute('SELECT * FROM production_nodes').fetchone()
        class RenderConnection:
            def __getattr__(_self, name): return getattr(self.database, name)
            def close(_self): pass
        async def mix(_concat, _bgm, output, **_kwargs): Path(output).write_bytes(b'actual-final')
        with patch.object(s6_compose, 'ASSETS_DIR', self.root/'data'/'assets'), \
             patch.object(s6_compose, 'conn', return_value=RenderConnection()), \
             patch.object(s6_compose.ffmpeg, 'probe_duration', new=AsyncMock(return_value=4)), \
             patch.object(s6_compose, '_prep_director_segment', new=AsyncMock(return_value=self.base/'prep.mp4')), \
             patch.object(s6_compose.ffmpeg, 'concat_shots', new=AsyncMock()), \
             patch.object(s6_compose.ffmpeg, 'mix_bgm', new=AsyncMock(side_effect=mix)), \
             patch.object(s6_compose.bigasr, 'transcribe_detailed', side_effect=AssertionError('ASR disabled')), \
             patch.dict('os.environ', {'APPEND_CTA':'false','GEN_THUMBNAIL':'false','GEN_MARKETING_THUMBNAIL':'false'}):
            output = asyncio.run(production_adapters.run_local_compose_node(self.database, node))
            self.assertEqual(output['final_path'], str(self.base/'final.mp4'))
            content_review.require_final(self.database, self.root/'data'/'assets', 'J')
            stored = json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
            self.assertEqual(stored['shots'][0]['video_master_idx'], 1)
            self.assertEqual(stored['production_settings']['budget_cents'], 9999)
            self.assertEqual(json.loads((self.base/'storyboard.json').read_text(encoding='utf-8')), stored)
            self.assertEqual(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0], encoded_spec)
            changed = deepcopy(stored)
            changed['visual_requirements'] = [{**REQS[0], 'description':'Changed requirement'}]
            changed['masters'][0]['visual_requirements'] = changed['visual_requirements']
            changed_json = json.dumps(changed)
            self.database.execute('UPDATE jobs SET storyboard=?', (changed_json,)); self.database.commit()
            with self.assertRaises(visual_evidence.ContentReviewRequired):
                asyncio.run(production_adapters.run_local_compose_node(self.database, node))
            self.assertEqual(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0], changed_json)
            self.assertEqual(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0], encoded_spec)
