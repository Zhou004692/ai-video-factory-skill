"""Offline source-bound native edits through the actual paid graph boundary."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import array
import threading
from http.server import ThreadingHTTPServer
from urllib import request,error
import httpx
import unittest
from unittest.mock import patch, AsyncMock

from factory import production_adapters, native_visual_repair, production_graph_runner, production_executor, production_invariants, content_review, visual_evidence
from factory.clients import ffmpeg, seedance, video_qa
from factory.steps import s3_shots
import test_content_review as fixture


class NativeRepairTests(unittest.TestCase):
    setUp = fixture.ContentReviewTests.setUp
    tearDown = fixture.ContentReviewTests.tearDown

    def graph(self):
        self.sb['masters'][0].update(type='dialogue', voiceover='Hello')
        spec = {'spec_hash':'frozen', 'runtime_storyboard':deepcopy(self.sb),
                'characters':[], 'nodes':[
                    {'node_id':'master:1','node_type':'master','input':{'master_idx':1}},
                    {'node_id':'local_compose','node_type':'local_compose','input':{}},
                    {'node_id':'final_review','node_type':'final_review','input':{}}], 'edges':[]}
        self.database.execute("UPDATE jobs SET execution_engine='graph_v2',production_spec_version=2,production_spec_hash='frozen',production_spec_json=?,storyboard=?,cost_cents=123", (json.dumps(spec),json.dumps(self.sb)))
        self.database.execute("UPDATE shots SET state='OK',cost_cents=123,duration_sec=4")
        for name, kind in [('master:1','master'),('local_compose','local_compose'),('final_review','final_review')]:
            self.database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,idempotency_key,input_json,actual_cost_cents) VALUES('J',?,?,'SUCCEEDED','frozen',?,?,?)",(name,kind,'original-'+name,json.dumps({'master_idx':1} if kind=='master' else {}),123 if kind=='master' else 0))
        self.database.commit()

    def media(self, path, color='red', seconds=4, tone=440, width=720, height=720, fps=24):
        subprocess.run([ffmpeg.executable('ffmpeg'),'-y','-loglevel','error','-f','lavfi','-i',f'color=c={color}:s={width}x{height}:r={fps}:d={seconds}',
            '-f','lavfi','-i',f'sine=frequency={tone}:duration={seconds}', '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac',str(path)],check=True,capture_output=True)

    def prepare(self, source='active',kind='edit'):
        args={'source':source,'source_confirmed':source=='candidate','expected_preview_identity':self.preview(source)}
        if kind=='extend': return self.service.prepare_master_extension('J',1,extension_sec=4,direction='Continue to conclusion',continuation_only_confirmed=True,**args)
        return self.service.prepare_master_lip_sync_repair('J',1,start_sec=1,end_sec=2,reason='Fix gesture without changing identity',**args)

    def start(self, attempt):
        with patch.object(self.service,'_require_transport_preflight'), patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}):
            return self.service.start_video_job('J',confirmed=True,approved_budget_cents=attempt['required_cumulative_budget_cents'],
                native_repair_attempt_id=attempt['attempt_id'],native_repair_request_fingerprint=attempt['request_fingerprint'])
    def preview(self,source='active'):
        return self.service.content_review_detail('J',1,source)['repair_preview_identity']

    def execute(self, attempt, provider, *, full=True, hard_qa=False):
        calls=[]; uploaded={}
        def transport(req):
            calls.append((req.method,str(req.url),json.loads(req.content) if req.content else None))
            if req.method=='POST':
                self.assertFalse(self.database.in_transaction,'POST must follow committed archive/authorization')
                sealed=native_visual_repair.archived_source_bytes(self.database,self.root/'data'/'assets',attempt)
                self.assertEqual(len(sealed),attempt['source_guards']['media']['size_bytes'])
                return httpx.Response(200,json={'id':'offline-'+attempt['attempt_id']})
            if '/tasks/' in str(req.url): return httpx.Response(200,json={'status':'succeeded','content':{'video_url':'https://8.8.8.8/media'},'usage':{'cost_cents':attempt['estimated_cost_cents']}})
            return httpx.Response(200,content=provider.read_bytes())
        real_client=httpx.AsyncClient
        def client(*args,**kwargs): return real_client(*args,**kwargs,transport=httpx.MockTransport(transport))
        def upload(path,**kwargs):
            uri='https://offline.invalid/qa/'+Path(path).name; uploaded[uri]=Path(path).read_bytes(); return uri
        with patch.object(s3_shots,'ASSETS_DIR',self.root/'data'/'assets'), \
             patch.object(s3_shots,'conn',return_value=self.database), \
             patch.object(seedance.httpx,'AsyncClient',side_effect=client), \
             patch.object(seedance.asyncio,'sleep',new=AsyncMock()), \
             patch.dict('os.environ',{'ARK_API_KEY':'offline'}), \
             patch.object(s3_shots.tos,'upload',side_effect=upload), \
             patch.object(video_qa,'analyze',return_value=({},0)) as qa, \
             patch.object(video_qa,'evaluate',return_value=video_qa.QADecision(not hard_qa,('unexpected_readable_text',) if hard_qa else ())), \
             patch.object(s3_shots.bigasr,'transcribe_detailed',side_effect=AssertionError('No ASR opted in')):
            result=asyncio.run(production_graph_runner.run_graph_job(self.database,'J',configured_limit=1,handlers=production_adapters.build_handlers(self.database)))
        self.assertEqual(result.job_state,'PRODUCTION_REVIEW_REQUIRED',dict(self.database.execute("SELECT * FROM production_nodes WHERE node_type='native_repair'").fetchone()))
        self.assertEqual(result.started_nodes,('native_repair:'+attempt['attempt_id'],))
        candidate=Path(content_review.object_json(self.database.execute('SELECT video_qa FROM shots').fetchone()[0])['candidate_path'])
        if full:self.assertEqual(uploaded[qa.call_args.args[0]],candidate.read_bytes(), 'QA must analyze the exact full assembled candidate')
        else:qa.assert_not_called()
        return calls,candidate

    def real_graph(self,kind='dialogue'):
        self.graph(); self.sb['production_settings']={'asr_enabled':False}
        self.sb['masters'][0]['type']=kind
        original=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]); original['runtime_storyboard']=deepcopy(self.sb)
        self.database.execute('UPDATE jobs SET production_spec_json=?,storyboard=?',(json.dumps(original),json.dumps(self.sb))); self.database.commit()
        self.media(self.active); self.media(self.candidate,color='green')

    def test_real_prepare_start_graph_s3_http_edit_for_dialogue_and_broll(self):
        for kind in ['dialogue','broll']:
            with self.subTest(kind=kind):
                if kind=='broll': self.tearDown(); self.setUp()
                self.real_graph(kind)
                original=self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]
                attempt=self.prepare(); before=self.active.read_bytes()
                provider=self.base/'reply.mov';self.media(provider,color='blue',tone=880)
                self.start(attempt)
                calls,candidate=self.execute(attempt,provider)
                self.assertEqual(self.active.read_bytes(),before)
                body=next(c[2] for c in calls if c[0]=='POST')
                self.assertEqual(body['omni_reference_task_type'],'edit'); self.assertEqual(body['duration'],-1)
                self.assertTrue(body['content'][1]['video_url']['url'].startswith('data:video/mp4;base64,'))
                self.assertEqual(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0],original)
                self.assertEqual(self.database.execute("SELECT state,actual_cost_cents FROM production_nodes WHERE node_id='master:1'").fetchone()[:],('SUCCEEDED',123))
                self.assertEqual(self.start(attempt)['already_started'],True)
                self.assertEqual(sum(c[0]=='POST' for c in calls),1)

    def test_prepare_rejects_nan_out_of_bounds_and_unconfirmed_candidate(self):
        self.real_graph()
        for start,end in [(float('nan'),2),(-1,2),(1,float('inf')),(1,9)]:
            with self.assertRaises(ValueError): self.service.prepare_master_lip_sync_repair('J',1,start_sec=start,end_sec=end,reason='Fix')
        with self.assertRaisesRegex(ValueError,'explicit'): self.service.prepare_master_lip_sync_repair('J',1,start_sec=1,end_sec=2,reason='Fix',source='candidate')
        self.assertEqual(self.database.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0],0)

    def test_stale_source_budget_and_forged_attempt_fail_before_post(self):
        self.real_graph(); attempt=self.prepare()
        self.active.write_bytes(b'changed')
        with self.assertRaises(Exception): self.start(attempt)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM production_nodes WHERE node_type='native_repair'").fetchone()[0],0)
        self.media(self.active); self.start(attempt)
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_type='native_repair'").fetchone()
        self.database.execute("UPDATE production_nodes SET input_json='{}' WHERE node_type='native_repair'");self.database.commit()
        self.assertTrue(production_invariants.audit(self.database,'J').blockers)

    def test_preparation_and_start_do_not_commit_outer_transaction_or_dispatch(self):
        self.real_graph(); self.database.execute('BEGIN')
        attempt=self.prepare()
        with patch.object(self.service,'_require_transport_preflight'),patch.object(self.service,'dispatch_production_queue') as dispatch:
            result=self.service.start_video_job('J',confirmed=True,approved_budget_cents=attempt['required_cumulative_budget_cents'],
                native_repair_attempt_id=attempt['attempt_id'],native_repair_request_fingerprint=attempt['request_fingerprint'])
        self.assertTrue(self.database.in_transaction);self.assertTrue(result['dispatch_deferred']); dispatch.assert_not_called()
        self.database.rollback()
        self.assertIsNone(native_visual_repair.current(self.database,'J'))

    def pixel(self,path,second):
        r=subprocess.run([ffmpeg.executable('ffmpeg'),'-loglevel','error','-ss',str(second),'-i',str(path),'-frames:v','1','-vf','scale=1:1','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],capture_output=True,check=True)
        return tuple(r.stdout[:3])

    def tone(self,path):
        r=subprocess.run([ffmpeg.executable('ffmpeg'),'-loglevel','error','-i',str(path),'-t','1','-map','0:a:0','-ar','8000','-ac','1','-f','s16le','pipe:1'],capture_output=True,check=True)
        samples=array.array('h');samples.frombytes(r.stdout)
        return sum(samples[n]<0<=samples[n+1] for n in range(len(samples)-1))

    def test_extend_assembles_full_prefix_and_original_native_audio_before_qa(self):
        self.real_graph('broll'); attempt=self.prepare(kind='extend')
        self.assertEqual(attempt['input_duration_sec'],4);self.assertEqual(attempt['provider_output_duration_sec'],4)
        self.assertEqual(attempt['retained_candidate_duration_sec'],8)
        self.start(attempt); provider=self.base/'tail.mov';self.media(provider,color='blue',tone=880)
        calls,candidate=self.execute(attempt,provider)
        self.assertEqual(next(c[2] for c in calls if c[0]=='POST')['omni_reference_task_type'],'extend')
        info=json.loads(asyncio.run(ffmpeg._ffprobe(['-show_streams','-show_format','-of','json',str(candidate)])))
        picture=next(s for s in info['streams'] if s['codec_type']=='video')
        self.assertEqual(picture['codec_name'],'h264');self.assertEqual(picture['pix_fmt'],'yuv420p')
        self.assertAlmostEqual(float(picture['duration']),8,places=2)
        self.assertGreater(self.pixel(candidate,1)[0],200); self.assertGreater(self.pixel(candidate,6)[2],200)
        self.assertTrue(430<self.tone(candidate)<450,'Original 440Hz audio must survive, not provider 880Hz')

    def test_candidate_source_then_fresh_accept_and_keep_prior_close_only_repair(self):
        for decision in ['accept_candidate','keep_prior']:
            with self.subTest(decision=decision):
                if decision=='keep_prior': self.tearDown();self.setUp()
                self.real_graph();self.database.execute("UPDATE shots SET state='REVIEW_REQUIRED'")
                self.database.execute("UPDATE production_nodes SET state='REVIEW_REQUIRED' WHERE node_id='master:1'");self.database.commit()
                original_candidate=self.candidate.read_bytes(); original_active=self.active.read_bytes()
                attempt=self.prepare(source='candidate'); self.start(attempt)
                provider=self.base/'reply.mov';self.media(provider,color='blue',tone=880)
                _,candidate=self.execute(attempt,provider)
                self.assertEqual(self.candidate.read_bytes(),original_candidate)
                self.assertNotEqual(candidate,self.candidate)
                with self.assertRaisesRegex(ValueError,'content'):
                    self.service.review_master_candidate('J',1,decision=decision)
                target=candidate if decision=='accept_candidate' else self.active
                payload=fixture.ContentReviewTests.payload(self,'active' if decision=='keep_prior' else 'candidate')
                payload['expected_asset_sha256']=visual_evidence.asset_fingerprint(target)
                self.service.record_content_review('J',1,payload)
                with patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}),patch.object(s3_shots.ffmpeg,'probe_duration',new=AsyncMock(return_value=4)),patch('factory.management_api.s6_compose.run',new=AsyncMock()):
                    result=self.service.review_master_candidate('J',1,decision=decision)
                self.assertEqual(result['decision'],decision)
                self.assertIsNone(native_visual_repair.current(self.database,'J'))
                self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_type='native_repair'").fetchone()[0],'SUCCEEDED')
                if decision=='keep_prior':self.assertEqual(self.active.read_bytes(),original_active)

    def test_real_edit_preserves_prefix_suffix_audio_and_fractional_picture(self):
        self.real_graph(); self.media(self.active,seconds=4.25); attempt=self.prepare()
        self.assertEqual(attempt['estimated_cost_cents'],round(8.5*153))
        self.start(attempt); provider=self.base/'short.mov';self.media(provider,seconds=3.9,color='blue',tone=880)
        _,candidate=self.execute(attempt,provider)
        self.assertGreater(self.pixel(candidate,.5)[0],200);self.assertGreater(self.pixel(candidate,1.5)[2],200);self.assertGreater(self.pixel(candidate,4.1)[0],200)
        self.assertTrue(430<self.tone(candidate)<450)
        picture=json.loads(asyncio.run(ffmpeg._ffprobe(['-show_streams','-of','json',str(candidate)])))['streams'][0]
        self.assertAlmostEqual(float(picture['duration']),4.25,places=2)

    def test_failed_target_coverage_preserves_raw_candidate_without_approval(self):
        self.real_graph(); attempt=self.service.prepare_master_lip_sync_repair('J',1,start_sec=3,end_sec=4,reason='Repair critical ending',expected_preview_identity=self.preview())
        self.start(attempt); provider=self.base/'short.mov';self.media(provider,seconds=3.6,color='blue')
        # Assembly cannot claim retention; the raw paid artifact remains reviewable.
        with patch.object(native_visual_repair,'assemble',wraps=native_visual_repair.assemble) as assembly:
            # execute helper expects QA on full bytes; use the same transport path,
            # but raw insufficient output deliberately never reaches machine QA.
            self.execute(attempt,provider,full=False)
        qa=content_review.object_json(self.database.execute('SELECT video_qa FROM shots').fetchone()[0])
        self.assertFalse(qa['native_repair_full_candidate']);self.assertTrue(Path(qa['candidate_path']).is_file())
        payload=fixture.ContentReviewTests.payload(self)
        payload['expected_asset_sha256']=visual_evidence.asset_fingerprint(Path(qa['candidate_path']))
        payload['items'][0].update(status='not_applicable',reason='Human visual waiver')
        self.service.record_content_review('J',1,payload)
        with self.assertRaisesRegex(ValueError,'raw native'):
            self.service.review_master_candidate('J',1,decision='accept_candidate')
        self.assertEqual(self.database.execute('SELECT COUNT(*) FROM provider_calls WHERE task_id NOT LIKE \'reservation:%\'').fetchone()[0],1)

    def test_known_owned_paid_restart_retrieves_only_and_does_not_double_cost(self):
        self.real_graph();attempt=self.prepare();self.start(attempt)
        provider=self.base/'reply.mov';self.media(provider,color='blue');calls,candidate=self.execute(attempt,provider)
        total=self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0]
        # Simulate a committed remote result but lost handler checkpoint.
        self.database.execute("UPDATE production_nodes SET state='ACTION_REQUIRED',output_json='{}' WHERE node_type='native_repair'");self.database.commit()
        task=self.database.execute("SELECT provider_task_id FROM production_nodes WHERE node_type='native_repair'").fetchone()[0]
        with patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}):
            result=self.service.reconcile_provider_task('J',task)
        self.assertTrue(result['get_only'])
        with patch.object(seedance,'_video_task_payload',side_effect=AssertionError('Historical recovery must bypass new submission rules')),patch.object(native_visual_repair,'probe_source',side_effect=AssertionError('GET first before new source preflight')):
            recovered,_=self.execute(attempt,provider)
        self.assertFalse(any(c[0]=='POST' for c in recovered));self.assertTrue(any('/tasks/' in c[1] for c in recovered))
        self.assertEqual(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0],total)

    def review_candidate(self,decision):
        qa=content_review.object_json(self.database.execute('SELECT video_qa FROM shots').fetchone()[0])
        target=Path(qa['candidate_path']) if decision=='accept_candidate' else self.active
        payload=fixture.ContentReviewTests.payload(self,'candidate' if decision=='accept_candidate' else 'active')
        payload['expected_asset_sha256']=visual_evidence.asset_fingerprint(target)
        self.service.record_content_review('J',1,payload)
        with patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}), \
             patch.object(s3_shots.ffmpeg,'probe_duration',new=AsyncMock(return_value=8)), \
             patch('factory.management_api.s6_compose.run',new=AsyncMock()):
            return self.service.review_master_candidate('J',1,decision=decision)

    def queue_owned_recovery(self):
        self.database.execute("UPDATE production_nodes SET state='ACTION_REQUIRED',output_json='{}' WHERE node_type='native_repair'")
        self.database.commit()
        task=self.database.execute("SELECT provider_task_id FROM production_nodes WHERE node_type='native_repair'").fetchone()[0]
        with patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}):
            self.assertTrue(self.service.reconcile_provider_task('J',task)['get_only'])

    def test_assembly_source_archive_survives_original_ABA_and_get_only_restart(self):
        import base64
        self.real_graph('broll');attempt=self.prepare();self.start(attempt);original=self.active.read_bytes()
        replacement=self.base/'replacement.mp4';self.media(replacement,color='green',tone=880)
        provider=self.base/'reply.mov';self.media(provider,color='blue',tone=1200)
        assemble=native_visual_repair.assemble
        async def mutate_original(bound,source,raw,output):
            source.write_bytes(replacement.read_bytes())
            try:return await assemble(bound,source,raw,output)
            finally:source.write_bytes(original)
        with patch.object(native_visual_repair,'assemble',side_effect=mutate_original):
            calls,candidate=self.execute(attempt,provider)
        body=next(call[2] for call in calls if call[0]=='POST')
        sent=next(item['video_url']['url'] for item in body['content'] if item.get('role')=='reference_video')
        self.assertEqual(base64.b64decode(sent.split(',',1)[1]),original)
        archive=native_visual_repair.source_archive_path(self.root/'data'/'assets',attempt)
        self.assertEqual(archive.read_bytes(),original)
        self.assertGreater(self.pixel(candidate,.5)[0],200);self.assertTrue(430<self.tone(candidate)<450)
        self.assertEqual(self.active.read_bytes(),original)
        total=self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0]
        self.active.write_bytes(replacement.read_bytes());self.queue_owned_recovery()
        with patch.object(native_visual_repair,'probe_source',side_effect=AssertionError('Recovery cannot apply new submission profile')):
            recovered,candidate=self.execute(attempt,provider)
        self.assertFalse(any(call[0]=='POST' for call in recovered))
        self.assertGreater(self.pixel(candidate,.5)[0],200);self.assertTrue(430<self.tone(candidate)<450)
        self.assertEqual(self.active.read_bytes(),replacement.read_bytes())
        self.assertEqual(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0],total)

    def test_missing_or_corrupt_archive_recovery_retains_raw_without_full_proof(self):
        for corruption in ['missing','changed']:
            with self.subTest(corruption=corruption):
                if corruption=='changed':self.tearDown();self.setUp()
                self.real_graph();attempt=self.prepare();self.start(attempt)
                provider=self.base/'reply.mov';self.media(provider,color='blue')
                self.execute(attempt,provider);total=self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0]
                archive=native_visual_repair.source_archive_path(self.root/'data'/'assets',attempt)
                if corruption=='missing':archive.unlink()
                else:archive.write_bytes(b'corrupt archive')
                self.queue_owned_recovery();calls,candidate=self.execute(attempt,provider,full=False)
                self.assertFalse(any(call[0]=='POST' for call in calls));self.assertTrue(candidate.is_file())
                qa=content_review.object_json(self.database.execute('SELECT video_qa FROM shots').fetchone()[0])
                self.assertFalse(qa['native_repair_full_candidate'])
                with self.assertRaisesRegex(ValueError,'raw native'):
                    self.service.review_master_candidate('J',1,decision='accept_candidate')
                self.assertEqual(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0],total)

    def test_extension_then_keep_prior_uses_active_historical_bounds_in_real_local_adapter(self):
        self.real_graph('broll');first=self.prepare(kind='extend');self.start(first)
        provider=self.base/'tail.mov';self.media(provider,color='blue')
        self.execute(first,provider);self.review_candidate('accept_candidate')
        self.database.execute("UPDATE production_nodes SET state='SUCCEEDED' WHERE node_id IN ('local_compose','final_review')")
        self.database.execute("UPDATE jobs SET state='COMPOSED'");self.database.commit()
        extended=self.active.read_bytes()
        second=self.prepare();self.start(second)
        replacement=self.base/'edit.mov';self.media(replacement,color='yellow',seconds=8)
        self.execute(second,replacement)
        frozen=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0])['runtime_storyboard']
        projected=native_visual_repair.accepted_storyboard(self.database,'J',frozen)
        self.assertEqual(projected['masters'][0]['duration'],8,'Pending candidate cannot replace active accepted bounds')
        self.review_candidate('keep_prior');self.assertEqual(self.active.read_bytes(),extended)
        compose=AsyncMock();node=self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
        with patch('factory.production_adapters.s6_compose.run',new=compose):
            with self.assertRaisesRegex(RuntimeError,'no final video'):
                asyncio.run(production_adapters.run_local_compose_node(self.database,node))
        projected=compose.call_args.kwargs['storyboard_override']
        live=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
        content_review.persist_effective_storyboard(self.database,'J',projected,
            expected_local_fingerprint=content_review.edit_plan_fingerprint(live))
        persisted=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
        self.assertEqual(projected['masters'][0]['duration'],8);self.assertEqual(persisted['masters'][0]['duration'],8)
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='final_review'").fetchone()[0],'PENDING')

    def test_cleanup_accept_retry_is_read_only_and_changed_active_is_stale(self):
        from factory import review_candidates
        self.real_graph();attempt=self.prepare();self.start(attempt)
        provider=self.base/'reply.mov';self.media(provider,color='blue')
        self.execute(attempt,provider);self.review_candidate('accept_candidate')
        accepted=self.active.read_bytes()
        self.assertEqual(review_candidates.finalize_accepted_candidates(self.database,self.root/'data'/'assets','J'),1)
        before='\n'.join(self.database.iterdump())
        with patch.object(self.service,'dispatch_production_queue') as dispatch:
            self.assertTrue(self.service.review_master_candidate('J',1,decision='accept_candidate')['already_committed'])
        dispatch.assert_not_called();self.assertEqual(before,'\n'.join(self.database.iterdump()))
        self.media(self.active,color='green')
        with self.assertRaises(ValueError):self.service.review_master_candidate('J',1,decision='accept_candidate')
        self.assertEqual(before,'\n'.join(self.database.iterdump()))
        self.active.write_bytes(accepted)
        self.database.execute("UPDATE production_nodes SET state='SUCCEEDED' WHERE node_id IN ('local_compose','final_review')")
        self.database.execute("UPDATE jobs SET state='COMPOSED'");self.database.commit()
        later=self.prepare();self.start(later);self.media(provider,color='yellow')
        self.execute(later,provider)
        before='\n'.join(self.database.iterdump())
        with self.assertRaises(ValueError):self.service.review_master_candidate('J',1,decision='accept_candidate')
        self.assertEqual(before,'\n'.join(self.database.iterdump()))

    def test_archive_media_must_match_confirmed_scope_before_post(self):
        self.real_graph();attempt=self.prepare();self.start(attempt)
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_type='native_repair'").fetchone()
        actual=native_visual_repair.probe_source
        def wrong_archived_media(path):
            result=actual(path)
            if str(path).endswith('source.mp4'):result={**result,'fps':60}
            return result
        provider=self.base/'reply.mov';self.media(provider)
        with patch.object(native_visual_repair,'probe_source',side_effect=wrong_archived_media):
            with self.assertRaises(AssertionError):self.execute(attempt,provider)
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_type='native_repair'").fetchone()
        self.assertIn('confirmed scope/cost',node['output_json']);self.assertFalse(node['provider_task_id'])
        self.assertEqual(self.database.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0],0)
        self.assertEqual(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0],123)

    def test_actual_before_post_guard_rejects_operator_budget_change(self):
        self.real_graph();attempt=self.prepare();self.start(attempt)
        original=seedance._video_task_payload
        def mutate(**kwargs):
            payload=original(**kwargs)
            sb=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]);sb['production_settings']['budget_cents']+=1
            self.database.execute('UPDATE jobs SET storyboard=?',(json.dumps(sb),));self.database.commit()
            return payload
        provider=self.base/'reply.mov';self.media(provider)
        with patch.object(seedance,'_video_task_payload',side_effect=mutate):
            with self.assertRaises(AssertionError):self.execute(attempt,provider)
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_type='native_repair'").fetchone()
        self.assertIn('budget changed',node['output_json'])
        self.assertFalse(node['provider_task_id']);self.assertEqual(node['actual_cost_cents'],0)
        self.assertEqual(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0],123)

    def test_forged_extra_edge_and_non_authorized_node_block_worker(self):
        self.real_graph();attempt=self.prepare();self.start(attempt)
        node_id='native_repair:'+attempt['attempt_id']
        self.database.execute("INSERT INTO production_edges(job_id,from_node_id,to_node_id,edge_type) VALUES('J',?,'final_review','native_repair')",(node_id,));self.database.commit()
        self.assertIn('native_repair_authorization_invalid',{i.code for i in production_invariants.audit(self.database,'J').blockers})
        self.database.execute("DELETE FROM production_edges WHERE to_node_id='final_review'")
        self.database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,idempotency_key) VALUES('J','unauthorized','master','READY','frozen','forged')");self.database.commit()
        self.assertTrue(production_invariants.audit(self.database,'J').blockers)

    def test_normal_reference_discriminator_keeps_billing_intent_and_legacy_neighbors(self):
        from test_seedance_submission_contracts import payload
        reference=payload(('reference_video',),task_intent='generate')
        self.assertEqual(reference['omni_reference_task_type'],'reference')
        for roles in [(),('first_frame','last_frame')]:
            self.assertNotIn('omni_reference_task_type',payload(roles))
        self.assertNotIn('omni_reference_task_type',payload(('reference_video',),model='doubao-seedance-2-0-260128'))

    def test_high_resolution_source_low_requested_output_has_consistent_area_bound(self):
        self.real_graph()
        self.sb['production_settings']={'asr_enabled':False,'generation_snapshot':{'resolution':'480p'}}
        self.database.execute('UPDATE jobs SET storyboard=?',(json.dumps(self.sb),));self.database.commit()
        self.media(self.active,width=1920,height=1080)
        attempt=self.prepare();self.start(attempt)
        self.assertEqual(attempt['estimated_cost_cents'],8*344)
        self.assertEqual(attempt['estimate_basis']['source_pixels'],1920*1080)
        request=s3_shots._lip_sync_edit_request_kwargs({'repair_source_path':str(self.active),'duration_sec':4,'generation_snapshot':{'resolution':'480p'}})
        request.update(native_input_rate_cents_per_second=344,native_output_rate_cents_per_second=344)
        self.assertEqual(s3_shots._estimated_request_cost_cents(request),attempt['estimated_cost_cents'])
        provider=self.base/'reply.mov';self.media(provider,color='blue')
        calls,_=self.execute(attempt,provider)
        body=next(c[2] for c in calls if c[0]=='POST')
        self.assertEqual(body['resolution'],'480p')
        self.assertNotIn('native_input_rate_cents_per_second',body)

    def test_hard_qa_failure_retains_only_paid_candidate_and_never_retries(self):
        self.real_graph();attempt=self.prepare();self.start(attempt)
        sb=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]);sb['production_settings']['generation_snapshot']['max_auto_retries']=1
        self.database.execute('UPDATE jobs SET storyboard=?',(json.dumps(sb),));self.database.commit()
        provider=self.base/'reply.mov';self.media(provider,color='blue')
        calls,candidate=self.execute(attempt,provider,hard_qa=True)
        self.assertEqual(sum(c[0]=='POST' for c in calls),1);self.assertTrue(candidate.is_file())
        self.assertGreater(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0],123)
        qa=content_review.object_json(self.database.execute('SELECT video_qa FROM shots').fetchone()[0])
        self.assertEqual(Path(qa['native_raw_provider_path']).read_bytes(),provider.read_bytes())

    def test_failed_browser_conversion_preserves_paid_mov_and_cost_without_retry(self):
        self.real_graph();attempt=self.prepare();self.start(attempt)
        provider=self.base/'reply.mov';self.media(provider,color='blue')
        with patch.object(ffmpeg,'transcode_edit_mov_to_mp4',new=AsyncMock(side_effect=ffmpeg.FFmpegError('offline conversion failure'))):
            calls,candidate=self.execute(attempt,provider,full=False)
        self.assertEqual(candidate.read_bytes(),provider.read_bytes());self.assertEqual(candidate.suffix,'.mov')
        self.assertEqual(sum(c[0]=='POST' for c in calls),1)
        self.assertGreater(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0],123)

    def test_real_http_preparation_source_preview_and_existing_budget_start(self):
        import management_server
        self.real_graph(); management_server.ConsoleHandler.service=self.service
        server=ThreadingHTTPServer(('127.0.0.1',0),management_server.ConsoleHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}/api/videos/J'
        def post(path,body):
            response=request.urlopen(request.Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST'))
            return json.load(response)
        try:
            attempt=post('/masters/1/lip-sync-repair',{'repair_start_sec':1,'repair_end_sec':2,'reason':'Fix hand','source':'candidate','source_confirmed':True,'expected_preview_identity':self.preview('candidate')})
            self.assertEqual(request.urlopen(base+'/masters/1/repair-source').read(),self.candidate.read_bytes())
            with patch.object(self.service,'_require_transport_preflight'),patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}):
                result=post('/produce',{'confirmed':True,'approved_budget_cents':attempt['required_cumulative_budget_cents'],
                    'native_repair_attempt_id':attempt['attempt_id'],'native_repair_request_fingerprint':attempt['request_fingerprint']})
            self.assertEqual(result['attempt_id'],attempt['attempt_id'])
            self.assertEqual(self.database.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0],0)
        finally:server.shutdown();server.server_close();thread.join()

    def test_node_executes_source_prepare_then_explicit_budget_button(self):
        script=r'''
const fs=require('fs'), vm=require('vm');
const source=fs.readFileSync('scripts/management_ui/app.js','utf8');
const fragment=source.slice(source.indexOf('function confirmLipSyncRepair('),source.indexOf('function masterRepairActions('));
const elements={}; const calls=[];let html='';
const attempt={attempt_id:'attempt-A',request_fingerprint:'scope-A',master_idx:1,source_guards:{source:'candidate',current_cost_cents:123},direction:'Fix hand',start_sec:1,end_sec:2,affected_requirement_ids:['hand'],estimate_basis:{input_seconds:4,provider_output_seconds:4,input_rate_cents_per_second:153,output_rate_cents_per_second:153,area_rate_cents_per_second:153},estimated_cost_cents:1224,required_cumulative_budget_cents:1347,retained_candidate_duration_sec:4};
const context={console,Number,JSON,encodeURIComponent,esc:String,money:String,detailRow:(a,b)=>a+b,openModal:(_a,_b,text)=>{html=text},$:id=>elements[id]||(elements[id]={value:'',checked:false}),closeModal:()=>{},closeDrawer:()=>{},toast:()=>{},loadPage:()=>{},api:async(path,options)=>{if(!options)return {preview_url:'/preview?source=candidate',repair_preview_identity:{source:'candidate',asset_sha256:'preview-A'}};calls.push([path,JSON.parse(options.body)]);return attempt}};
vm.createContext(context);vm.runInContext(fragment,context);
for(const id of ['#native-source-confirmed','#native-direction','#native-start','#native-end','#native-budget'])context.$(id);
(async()=>{context.confirmLipSyncRepair({job_id:'J',execution_engine:'graph_v2'},{idx:1,duration_sec:4,candidate_preview_url:'/candidate'});elements['#native-source'].value='candidate';await elements['#native-source'].onchange();elements['#native-source-confirmed'].checked=true;elements['#native-direction'].value='Fix hand';elements['#native-start'].value='1';elements['#native-end'].value='2';await elements['#native-prepare'].onclick();if(calls.length!==1||calls[0][0].includes('produce'))throw Error('Preparation auto purchased');elements['#native-budget'].value='13.47';await elements['#native-budget-confirm'].onclick();console.log(JSON.stringify(calls));})().catch(e=>{console.error(e);process.exit(1)});
'''
        result=subprocess.run(['node','-e',script],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        calls=json.loads(result.stdout.strip())
        self.assertEqual(calls[0][1]['source'],'candidate');self.assertTrue(calls[0][1]['source_confirmed'])
        self.assertEqual(calls[1][0],'/api/videos/J/produce');self.assertEqual(calls[1][1]['approved_budget_cents'],1347)
        self.assertEqual(calls[0][1]['expected_preview_identity']['asset_sha256'],'preview-A')
        self.assertEqual(calls[1][1]['native_repair_attempt_id'],'attempt-A')
        self.assertEqual(calls[1][1]['native_repair_request_fingerprint'],'scope-A')

    def test_existing_prepare_then_frozen_adapter_must_execute_authorized_edit(self):
        self.graph()
        original = self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]
        with patch.object(native_visual_repair,'probe_source',return_value={'picture_duration_sec':4,'width':720,'height':720,'fps':24,'size_bytes':5,'has_audio':True}):
            result = self.service.prepare_master_lip_sync_repair('J',1,start_sec=1,end_sec=2,reason='Match mouth',expected_preview_identity=self.preview())
        node = self.database.execute("SELECT * FROM production_nodes WHERE node_id='master:1'").fetchone()
        with patch('factory.steps.s3_shots.run',new=AsyncMock()) as run:
            try: asyncio.run(production_adapters.run_master_node(self.database,node))
            except RuntimeError: pass  # The legacy preparation also reset the original shot.
        # Original execution remains unchanged; a separate frozen attempt owns the edit.
        self.assertEqual(run.await_args.kwargs['storyboard_override']['masters'][0],self.sb['masters'][0])
        self.assertIn('attempt_id',result, 'Native preparation must freeze an independent attempt instead of losing its mode at start')
        self.assertEqual(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0],original)

    def test_graph_preparation_requires_preview_identity(self):
        self.real_graph()
        with self.assertRaisesRegex(ValueError,'preview identity'):
            self.service.prepare_master_lip_sync_repair('J',1,start_sec=1,end_sec=2,reason='Fix',source='candidate',source_confirmed=True)

    def test_old_confirmation_cannot_authorize_new_same_price_attempt(self):
        self.real_graph();a=self.prepare();self.service.discard_native_repair_preparation('J')
        b=self.prepare()
        with self.assertRaisesRegex(ValueError,'attempt identity'):
            self.start(a)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM production_nodes WHERE node_type='native_repair'").fetchone()[0],0)

    def test_boolean_scope_is_not_a_numeric_interval(self):
        self.real_graph()
        with self.assertRaisesRegex(ValueError,'boolean'):
            self.service.prepare_master_lip_sync_repair('J',1,start_sec=False,end_sec=True,reason='Fix')

    def test_aba_source_swap_cannot_change_actual_submitted_bytes(self):
        import base64,hashlib
        self.real_graph();attempt=self.prepare();self.start(attempt)
        a=self.active.read_bytes();alternate=self.base/'alternate.mp4';self.media(alternate,color='blue');b=alternate.read_bytes()
        provider=self.base/'reply.mov';self.media(provider,color='blue');push=seedance._push_video
        def swap(content,role,path):
            self.active.write_bytes(b)
            try:return push(content,role,path)
            finally:self.active.write_bytes(a)
        with patch.object(seedance,'_push_video',side_effect=swap):calls,_=self.execute(attempt,provider)
        uri=next(c[2] for c in calls if c[0]=='POST')['content'][1]['video_url']['url']
        self.assertEqual(hashlib.sha256(base64.b64decode(uri.split(',',1)[1])).hexdigest(),attempt['source_guards']['asset_sha256'])

    def test_previewed_candidate_A_changed_to_B_is_rejected_without_mutation(self):
        self.real_graph();expected=self.preview('candidate');self.media(self.candidate,color='blue')
        before='\n'.join(self.database.iterdump())
        with self.assertRaisesRegex(ValueError,'preview identity'):
            self.service.prepare_master_lip_sync_repair('J',1,start_sec=1,end_sec=2,reason='Fix A',source='candidate',source_confirmed=True,expected_preview_identity=expected)
        self.assertEqual(before,'\n'.join(self.database.iterdump()))

    def test_preview_check_uses_frozen_snapshot_without_second_identity_read(self):
        self.real_graph();expected=self.preview('candidate');original=self.candidate.read_bytes()
        self.media(self.candidate,color='blue')
        before='\n'.join(self.database.iterdump());fingerprint=visual_evidence.asset_fingerprint;reads=0
        def restore_preview_after_snapshot_read(path):
            nonlocal reads
            value=fingerprint(path)
            if Path(path).resolve()==self.candidate.resolve():
                reads+=1
                if reads==1:self.candidate.write_bytes(original)
            return value
        with patch.object(visual_evidence,'asset_fingerprint',side_effect=restore_preview_after_snapshot_read):
            with self.assertRaisesRegex(ValueError,'preview identity'):
                self.service.prepare_master_lip_sync_repair('J',1,start_sec=1,end_sec=2,reason='Fix preview A',
                    source='candidate',source_confirmed=True,expected_preview_identity=expected)
        self.assertEqual(reads,1,'Authorization must compare the exact first snapshot, never re-read source identity')
        self.assertEqual(before,'\n'.join(self.database.iterdump()))

    def test_real_http_rejects_bool_stale_preview_and_old_budget_modal(self):
        import management_server
        self.real_graph();management_server.ConsoleHandler.service=self.service
        server=ThreadingHTTPServer(('127.0.0.1',0),management_server.ConsoleHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}/api/videos/J'
        def post(path,body):
            return json.load(request.urlopen(request.Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')))
        def rejected(path,body,text):
            before='\n'.join(self.database.iterdump())
            with self.assertRaises(error.HTTPError) as failed:post(path,body)
            self.assertEqual(failed.exception.code,400)
            self.assertIn(text,json.loads(failed.exception.read())['error'])
            self.assertEqual(before,'\n'.join(self.database.iterdump()))
        try:
            preview=json.load(request.urlopen(base+'/masters/1/content-review?source=candidate'))
            payload={'repair_start_sec':1,'repair_end_sec':2,'reason':'Fix A','source':'candidate','source_confirmed':True,'expected_preview_identity':preview['repair_preview_identity']}
            rejected('/masters/1/lip-sync-repair',{**payload,'repair_start_sec':False,'repair_end_sec':True},'boolean')
            rejected('/masters/1/extend',{'extension_sec':True,'direction':'Continue'},'boolean')
            self.media(self.candidate,color='blue')
            rejected('/masters/1/lip-sync-repair',payload,'preview identity')
            with self.assertRaises(error.HTTPError):request.urlopen(base+'/masters/1/content-preview?source=candidate&expected_sha256='+preview['asset_sha256'])
            payload['expected_preview_identity']=self.preview('candidate')
            a=post('/masters/1/lip-sync-repair',payload)
            post('/native-repair/discard',{'expected_attempt_id':a['attempt_id']})
            b=post('/masters/1/lip-sync-repair',{**payload,'reason':'Different scope B','repair_start_sec':2,'repair_end_sec':3})
            self.assertEqual(a['required_cumulative_budget_cents'],b['required_cumulative_budget_cents'])
            with patch.object(self.service,'_require_transport_preflight'),patch.object(self.service,'dispatch_production_queue',return_value={'dispatched_jobs':[]}):
                rejected('/produce',{'confirmed':True,'approved_budget_cents':a['required_cumulative_budget_cents'],'native_repair_attempt_id':a['attempt_id'],'native_repair_request_fingerprint':a['request_fingerprint']},'attempt identity')
                rejected('/produce',{'confirmed':True,'approved_budget_cents':b['required_cumulative_budget_cents']},'attempt identity')
            self.assertEqual(self.database.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0],0)
        finally:server.shutdown();server.server_close();thread.join()

    def test_60fps_source_declares_conservative_calibration_and_matches_runtime(self):
        self.real_graph();self.media(self.active,fps=60);attempt=self.prepare()
        basis=attempt['estimate_basis'];self.assertEqual(basis['source_fps'],60);self.assertEqual(basis['calibration_fps'],24)
        self.assertEqual(basis['fps_multiplier'],2.5);self.assertIn('not a verified',basis['fps_assumption'])
        self.assertEqual(attempt['estimated_cost_cents'],8*383)
        self.start(attempt);provider=self.base/'reply.mov';self.media(provider,color='blue')
        self.execute(attempt,provider)

    def test_unsubmitted_stale_rejection_can_discard_prepare_and_confirm_again(self):
        self.real_graph();attempt=self.prepare();self.start(attempt)
        sb=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]);sb['production_settings']['budget_cents']+=1
        self.database.execute('UPDATE jobs SET storyboard=?',(json.dumps(sb),));self.database.commit()
        provider=self.base/'reply.mov';self.media(provider,color='blue')
        with self.assertRaises(AssertionError):self.execute(attempt,provider)
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_type='native_repair'").fetchone()
        self.assertIsNone(node['provider_task_id']);self.assertEqual(node['actual_cost_cents'],0)
        self.service.discard_native_repair_preparation('J',expected_attempt_id=attempt['attempt_id'])
        fresh=self.prepare();self.assertNotEqual(fresh['attempt_id'],attempt['attempt_id']);self.start(fresh)
        calls,_=self.execute(fresh,provider)
        self.assertEqual(sum(c[0]=='POST' for c in calls),1)

    def test_discard_unsubmitted_started_attempt_does_not_restore_old_final_approval(self):
        self.real_graph();self.database.execute("UPDATE jobs SET state='READY_TO_PUBLISH'");self.database.commit()
        attempt=self.prepare();self.start(attempt)
        self.service.discard_native_repair_preparation('J',expected_attempt_id=attempt['attempt_id'])
        self.assertEqual(self.database.execute('SELECT state FROM jobs').fetchone()[0],'PRODUCTION_REVIEW_REQUIRED')
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='final_review'").fetchone()[0],'PENDING')
        fresh=self.prepare();self.assertNotEqual(fresh['attempt_id'],attempt['attempt_id'])


if __name__ == '__main__': unittest.main()
