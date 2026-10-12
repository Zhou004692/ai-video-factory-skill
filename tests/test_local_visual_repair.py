"""Offline repair routing and authorized free picture-window changes."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import unittest
import subprocess
import threading
from http.server import ThreadingHTTPServer
from urllib import request, error
from unittest.mock import AsyncMock, Mock, patch

from factory import content_review, master_transcriptions, node_state_machine, production_adapters, production_projection, visual_evidence
from factory.steps import s6_compose
import test_content_review as fixture


class LocalVisualRepairTests(unittest.TestCase):
    setUp = fixture.ContentReviewTests.setUp
    tearDown = fixture.ContentReviewTests.tearDown
    payload = fixture.ContentReviewTests.payload
    graph_pause = fixture.ContentReviewTests.graph_pause
    test_local_content_pause_has_review_entry = fixture.ContentReviewTests.test_local_content_pause_has_review_entry

    def prepare(self):
        self.graph_pause()
        self.sb['shots'] = [{'idx': 1, 'video_master_idx': 1, 'audio_master_idx': 1,
            'video_start': 2.5, 'audio_start': 0, 'duration': 1.5,
            'speech_mode': 'off_screen', 'continuity_relation': 'same_master_continuous'}]
        self.sb['production_settings'] = {'asr_enabled': True, 'budget_cents': 12345}
        spec = {'spec_hash': 'frozen', 'runtime_storyboard': deepcopy(self.sb)}
        self.database.execute("UPDATE jobs SET storyboard=?,production_spec_json=?,production_spec_hash='frozen',production_spec_version=2", (json.dumps(self.sb), json.dumps(spec)))
        self.database.commit()
        self.service.record_content_review('J', 1, self.payload('active'))

    def plan(self):
        return self.service.visual_repair_plan('J', 1, 'active')

    def command(self, plan):
        return {**plan['guards'], 'source': 'active', 'confirmed': True,
                'edits': plan['routes'][0]['plan']['edits']}

    def save(self):
        self.database.execute('UPDATE jobs SET storyboard=?', (json.dumps(self.sb),))
        self.database.commit()

    def test_readonly_recommendation_and_recut_reaches_frozen_adapter(self):
        self.prepare()
        before = '\n'.join(self.database.iterdump())
        plan = self.plan()
        self.assertEqual(before, '\n'.join(self.database.iterdump()))
        self.assertEqual(plan['recommended_route'], 'local_recut')
        result = self.service.apply_visual_recut('J', 1, self.command(plan))
        self.assertTrue(result['local_only'])
        stored = json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
        expected = stored['shots'][0]['video_start']
        self.assertLess(expected, 2.5)
        self.database.execute("UPDATE jobs SET final_path='already-local-final'")
        node = self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
        with patch.object(s6_compose, 'run', new=AsyncMock()) as run:
            asyncio.run(production_adapters.run_local_compose_node(self.database, node))
        effective = run.await_args.kwargs['storyboard_override']
        self.assertEqual(effective['shots'][0]['video_start'], expected)
        self.assertEqual(effective['shots'][0]['audio_start'], 0)
        self.assertEqual(stored['production_settings']['budget_cents'], 12345)
        self.assertEqual(self.database.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0], 0)

    def test_asr_sync_cannot_reset_authorized_safe_window(self):
        self.prepare()
        self.service.apply_visual_recut('J', 1, self.command(self.plan()))
        stored = json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
        expected = stored['shots'][0]['video_start']
        rows = self.database.execute('SELECT * FROM shots').fetchall()
        asyncio.run(s6_compose._refresh_director_speech_timeline('J', stored, rows, self.database))
        self.assertEqual(stored['shots'][0]['video_start'], expected)
        self.assertEqual(stored['shots'][0]['audio_start'], 0)
        self.assertEqual(stored['shots'][0]['duration'], 1.5)

    def test_actual_frozen_adapter_to_s6_render_matches_preview_window(self):
        environment = patch.dict('os.environ', {'APPEND_CTA':'false','GEN_THUMBNAIL':'false','GEN_MARKETING_THUMBNAIL':'false'})
        environment.start(); self.addCleanup(environment.stop)
        self.prepare()
        # A neighboring on-screen native-audio beat keeps one shared clock.
        self.sb['shots'].append({**self.sb['shots'][0], 'idx':2, 'speech_mode':'on_screen',
            'video_start':1.5, 'audio_start':1.5, 'duration':.5})
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]);spec['runtime_storyboard']=deepcopy(self.sb)
        self.database.execute('UPDATE jobs SET production_spec_json=?',(json.dumps(spec),));self.save()
        plan = self.plan()
        self.service.apply_visual_recut('J', 1, self.command(plan))
        node = self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
        spec_before = self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]
        class RenderConnection:
            def __getattr__(_self, name): return getattr(self.database, name)
            def close(_self): pass
        async def cut(_picture, _audio, output, **kwargs):
            Path(output).write_bytes(b'cut'); return output
        async def mix(_concat, _bgm, output, **_kwargs): Path(output).write_bytes(b'actual-final')
        with patch.object(s6_compose, 'ASSETS_DIR', self.root/'data'/'assets'), \
             patch.object(s6_compose, 'conn', return_value=RenderConnection()), \
             patch.object(s6_compose.ffmpeg, 'probe_duration', new=AsyncMock(return_value=4)), \
             patch.object(s6_compose.ffmpeg, 'cut_director_segment', new=AsyncMock(side_effect=cut)) as render, \
             patch.object(s6_compose.ffmpeg, 'concat_shots', new=AsyncMock()), \
             patch.object(s6_compose.ffmpeg, 'mix_bgm', new=AsyncMock(side_effect=mix)), \
             patch.object(s6_compose.bigasr, 'transcribe_detailed', side_effect=AssertionError('No speech needs ASR')), \
             patch.dict('os.environ', {'APPEND_CTA':'false','GEN_THUMBNAIL':'false','GEN_MARKETING_THUMBNAIL':'false'}):
            output = asyncio.run(production_adapters.run_local_compose_node(self.database, node))
            self.assertEqual(output['final_path'], str(self.base/'final.mp4'))
            args = render.await_args_list[0].kwargs
            self.assertEqual(args['video_start'], plan['routes'][0]['plan']['windows'][0]['proposed'][0])
            self.assertEqual(args['audio_start'], 0); self.assertEqual(args['duration'], 1.5)
            neighbor=render.await_args_list[1].kwargs
            self.assertEqual(neighbor['video_start'],1.5);self.assertEqual(neighbor['audio_start'],1.5);self.assertEqual(neighbor['duration'],.5)
            content_review.require_final(self.database, self.root/'data'/'assets', 'J')
            stored = json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
            self.assertEqual(stored, json.loads((self.base/'storyboard.json').read_text(encoding='utf-8')))
            self.assertEqual(stored['production_settings']['budget_cents'], 12345)
            self.assertEqual(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0], spec_before)

    def test_asr_executes_without_moving_approved_picture_or_audio_clocks(self):
        self.prepare(); self.sb['shots'][0]['subtitle'] = 'Hello'; self.save()
        transcription=s6_compose.bigasr.Transcription('Hello',100,1000,(s6_compose.bigasr.TimedUtterance('Hello',100,1000),))
        master_transcriptions.persist(self.database,'J',1,self.active,transcription);self.database.commit()
        self.service.apply_visual_recut('J',1,self.command(self.plan()))
        stored=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
        expected=stored['shots'][0]['video_start']; rows=self.database.execute('SELECT * FROM shots').fetchall()
        with patch.object(s6_compose,'ASSETS_DIR',self.root/'data'/'assets'), \
             patch.object(s6_compose.ffmpeg,'probe_duration',new=AsyncMock(return_value=4)), \
             patch.object(s6_compose.bigasr,'transcribe_detailed',return_value=transcription) as asr:
            asyncio.run(s6_compose._refresh_director_speech_timeline('J',stored,rows,self.database))
            asr.assert_not_called()
        self.assertEqual(stored['shots'][0]['video_start'],expected)
        self.assertEqual(stored['shots'][0]['audio_start'],0); self.assertEqual(stored['shots'][0]['duration'],1.5)
        self.assertTrue(stored['shots'][0]['subtitle_cues'])
        # A restart replays the new cues, with the same authorized picture.
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
        self.database.execute("UPDATE jobs SET final_path='final-existing'")
        with patch.object(s6_compose,'run',new=AsyncMock()) as run:
            asyncio.run(production_adapters.run_local_compose_node(self.database,node))
        self.assertEqual(run.await_args.kwargs['storyboard_override']['shots'],stored['shots'])

    def test_asr_cannot_implicitly_change_approved_audio_duration(self):
        self.prepare(); self.sb['shots'][0]['subtitle']='Long speech'; self.save()
        master_transcriptions.persist(self.database,'J',1,self.active,s6_compose.bigasr.Transcription('Short',0,1000,(s6_compose.bigasr.TimedUtterance('Short',0,1000),)));self.database.commit()
        self.service.apply_visual_recut('J',1,self.command(self.plan()))
        stored=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]); before=deepcopy(stored)
        rows=self.database.execute('SELECT * FROM shots').fetchall()
        transcription=s6_compose.bigasr.Transcription('Long speech',0,2000,(s6_compose.bigasr.TimedUtterance('Long speech',0,2000),))
        master_transcriptions.persist(self.database,'J',1,self.active,transcription);self.database.commit()
        with patch.object(s6_compose,'ASSETS_DIR',self.root/'data'/'assets'), \
             patch.object(s6_compose.ffmpeg,'probe_duration',new=AsyncMock(return_value=4)), \
             patch.object(s6_compose.bigasr,'transcribe_detailed',return_value=transcription):
            with self.assertRaises(visual_evidence.ContentReviewRequired):
                asyncio.run(s6_compose._refresh_director_speech_timeline('J',stored,rows,self.database))
        live=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
        self.assertEqual(live['shots'],before['shots'])

    def test_asr_recut_does_not_move_an_unselected_picture_window(self):
        self.prepare()
        self.sb['shots'].append({**self.sb['shots'][0], 'idx':2, 'duration':1., 'audio_start':1.5})
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0])
        spec['runtime_storyboard']=deepcopy(self.sb)
        self.database.execute('UPDATE jobs SET production_spec_json=?',(json.dumps(spec),));self.save()
        self.service.apply_visual_recut('J',1,self.command(self.plan()))
        stored=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]);before=deepcopy(stored['shots'])
        rows=self.database.execute('SELECT * FROM shots').fetchall()
        with patch.object(s6_compose,'ASSETS_DIR',self.root/'data'/'assets'):
            asyncio.run(s6_compose._refresh_director_speech_timeline('J',stored,rows,self.database))
        self.assertEqual(stored['shots'],before)

    def test_invalid_current_picture_or_audio_window_requires_review(self):
        self.prepare()
        for field,value in [('video_start',3),('video_start',float('nan')),('audio_start',3),('audio_start',float('nan'))]:
            with self.subTest(field=field,value=value):
                original=deepcopy(self.sb['shots'][0]); self.sb['shots'][0][field]=value;self.save()
                plan=self.plan()
                self.assertEqual(plan['routes'][0]['reason'],'invalid_or_unknown_timeline')
                self.assertFalse(plan['routes'][0]['executable_now'])
                self.sb['shots'][0]=original;self.save()

    def test_cached_alignment_has_finite_recovery_to_new_baseline_recut_without_provider_posts(self):
        environment=patch.dict('os.environ',{'APPEND_CTA':'false','GEN_THUMBNAIL':'false','GEN_MARKETING_THUMBNAIL':'false'})
        environment.start();self.addCleanup(environment.stop)
        self.prepare();self.sb['masters'][0]['type']='dialogue'
        self.sb['shots'][0].update(video_start=0,subtitle='Hello')
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]);spec['runtime_storyboard']=deepcopy(self.sb)
        self.database.execute('UPDATE jobs SET production_spec_json=?',(json.dumps(spec),));self.save()
        observation=self.payload('active');observation['items'][0].update(start_sec=2,end_sec=3)
        self.service.record_content_review('J',1,observation)
        short=s6_compose.bigasr.Transcription('Hello',0,1000,(s6_compose.bigasr.TimedUtterance('Hello',0,1000),))
        long=s6_compose.bigasr.Transcription('Hello',0,2000,(s6_compose.bigasr.TimedUtterance('Hello',0,2000),))
        master_transcriptions.persist(self.database,'J',1,self.active,short);self.database.commit()
        self.database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,cost_cents,usage_json) VALUES('already-paid','J',1,'seedance',777,?)",(json.dumps({'status':'succeeded'}),))
        self.database.execute('UPDATE jobs SET cost_cents=777');self.database.commit()
        ledger=[tuple(r) for r in self.database.execute('SELECT * FROM provider_calls')]
        self.service.apply_visual_recut('J',1,self.command(self.plan()))
        master_transcriptions.persist(self.database,'J',1,self.active,long);self.database.commit()
        class RenderConnection:
            def __getattr__(_self,name):return getattr(self.database,name)
            def close(_self):pass
        async def cut(_picture,_audio,output,**_kwargs):Path(output).write_bytes(b'cut')
        async def mix(_concat,_bgm,output,**_kwargs):Path(output).write_bytes(b'final-new-baseline')
        def run(epoch,expect_pause):
            node_state_machine.transition_node(self.database,'J','local_compose','start',event_id=f'cycle-{epoch}:start')
            node=self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
            if expect_pause:
                with self.assertRaises(visual_evidence.ContentReviewRequired):asyncio.run(production_adapters.run_local_compose_node(self.database,node))
                node_state_machine.transition_node(self.database,'J','local_compose','require_review',event_id=f'cycle-{epoch}:review')
                self.database.execute("UPDATE production_nodes SET error_code='content_review_required',next_action='review_visual_content' WHERE node_id='local_compose'")
                production_projection.persist(self.database,'J');self.database.commit()
            else:
                asyncio.run(production_adapters.run_local_compose_node(self.database,node))
        with patch.object(s6_compose,'ASSETS_DIR',self.root/'data'/'assets'), \
             patch.object(s6_compose,'conn',return_value=RenderConnection()), \
             patch.object(s6_compose.ffmpeg,'probe_duration',new=AsyncMock(return_value=4)), \
             patch.object(s6_compose.ffmpeg,'cut_director_segment',new=AsyncMock(side_effect=cut)) as render, \
             patch.object(s6_compose.ffmpeg,'concat_shots',new=AsyncMock()), \
             patch.object(s6_compose.ffmpeg,'mix_bgm',new=AsyncMock(side_effect=mix)), \
             patch.object(s6_compose.bigasr,'transcribe_detailed',side_effect=AssertionError('New ASR POST forbidden')) as asr:
            run(1,True)
            repair=self.plan();self.assertTrue(repair['local_alignment_recovery']['executable_now'])
            align={**repair['guards'],'source':'active','confirmed':True}
            self.service.align_cached_visual_timeline('J',1,align)
            self.assertTrue(self.service.align_cached_visual_timeline('J',1,align)['already_queued'])
            # Deleting the approved cache while this operation is queued must
            # never fall back to ASR POST. Restore it only to continue the test.
            self.database.execute('DELETE FROM master_transcriptions');self.database.commit()
            queued=self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
            with self.assertRaises(visual_evidence.ContentReviewRequired):asyncio.run(production_adapters.run_local_compose_node(self.database,queued))
            master_transcriptions.persist(self.database,'J',1,self.active,long);self.database.commit()
            run(2,True)  # Actual post-ASR retention rejects the newly cropped action.
            with self.assertRaisesRegex(ValueError,'locally recut'):self.service.resume_visual_content('J')
            new_sb=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
            self.assertNotIn('authorized_local_visual_edit',new_sb)
            self.assertTrue(new_sb['local_visual_edit_history'][0]['prior_overlay'])
            self.assertEqual(new_sb['shots'][0]['duration'],2.15)
            second=self.plan();self.assertTrue(second['routes'][0]['executable_now'])
            self.service.apply_visual_recut('J',1,self.command(second))
            run(3,False)
            args=render.await_args.kwargs
            self.assertAlmostEqual(args['video_start'],.85);self.assertEqual(args['audio_start'],0);self.assertEqual(args['duration'],2.15)
            content_review.require_final(self.database,self.root/'data'/'assets','J')
            asr.assert_not_called()
        self.assertEqual([tuple(r) for r in self.database.execute('SELECT * FROM provider_calls')],ledger)
        self.assertEqual(self.database.execute('SELECT cost_cents FROM jobs').fetchone()[0],777)
        self.assertEqual(self.bgm.read_bytes(),b'music')
        self.assertEqual(self.database.execute('SELECT bgm_path FROM jobs').fetchone()[0],str(self.bgm))
        self.assertEqual(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0],json.dumps(spec))

    def test_qa_transcription_or_wrong_asset_cache_does_not_authorize_free_alignment(self):
        self.prepare();self.sb['masters'][0]['type']='dialogue';self.sb['shots'][0]['subtitle']='Hello'
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]);spec['runtime_storyboard']=deepcopy(self.sb)
        self.database.execute('UPDATE jobs SET production_spec_json=?',(json.dumps(spec),));self.save()
        qa=json.loads(self.database.execute('SELECT video_qa FROM shots').fetchone()[0]);qa['transcription']={'text':'Hello'}
        self.database.execute('UPDATE shots SET video_qa=?',(json.dumps(qa),));self.database.commit()
        plan=self.plan();self.assertEqual(plan['routes'][0]['reason'],'speech_alignment_required_before_recut')
        self.assertFalse(plan['routes'][0]['executable_now']);self.assertFalse(plan['local_alignment_recovery']['executable_now'])
        with self.assertRaisesRegex(ValueError,'cached ASR'):
            self.service.align_cached_visual_timeline('J',1,{**plan['guards'],'source':'active','confirmed':True})
        master_transcriptions.persist(self.database,'J',1,self.candidate,s6_compose.bigasr.Transcription('Hello',0,1000));self.database.commit()
        self.assertEqual(self.plan()['cached_alignment']['missing_master_ids'],[1])

    def test_deleted_cache_after_approval_never_falls_back_to_new_asr_post(self):
        self.prepare();self.sb['masters'][0]['type']='dialogue';self.sb['shots'][0]['subtitle']='Hello'
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]);spec['runtime_storyboard']=deepcopy(self.sb)
        self.database.execute('UPDATE jobs SET production_spec_json=?',(json.dumps(spec),));self.save()
        master_transcriptions.persist(self.database,'J',1,self.active,s6_compose.bigasr.Transcription('Hello',0,1000));self.database.commit()
        self.service.apply_visual_recut('J',1,self.command(self.plan()))
        self.database.execute('DELETE FROM master_transcriptions');self.database.commit()
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
        with patch.object(s6_compose,'run',new=AsyncMock()) as run, \
             patch.object(s6_compose.bigasr,'transcribe_detailed',side_effect=AssertionError('New ASR POST forbidden')) as asr:
            with self.assertRaises(visual_evidence.ContentReviewRequired):asyncio.run(production_adapters.run_local_compose_node(self.database,node))
            run.assert_not_awaited();asr.assert_not_called()

    def test_cache_vanishes_at_actual_asr_dispatch_boundary_without_new_request(self):
        self.prepare();self.sb['shots'][0]['subtitle']='Hello';self.save()
        cached=s6_compose.bigasr.Transcription('Hello',0,1000)
        master_transcriptions.persist(self.database,'J',1,self.active,cached);self.database.commit()
        self.service.apply_visual_recut('J',1,self.command(self.plan()))
        stored=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]);rows=self.database.execute('SELECT * FROM shots').fetchall()
        with patch.object(master_transcriptions,'load_matching',side_effect=[cached,cached,None]), \
             patch.object(s6_compose.bigasr,'transcribe_detailed',side_effect=AssertionError('New ASR POST forbidden')) as asr:
            with self.assertRaises(visual_evidence.ContentReviewRequired):
                asyncio.run(s6_compose._refresh_director_speech_timeline('J',stored,rows,self.database))
            asr.assert_not_called()

    def test_replacement_cache_at_actual_consumption_is_rejected_before_subtitle_persistence(self):
        self.prepare();self.sb['shots'][0]['subtitle']='Approved';self.save()
        approved=s6_compose.bigasr.Transcription('Approved',0,1000,(s6_compose.bigasr.TimedUtterance('Approved',0,1000),))
        replacement=s6_compose.bigasr.Transcription('UNAPPROVED REPLACEMENT',0,1000,(s6_compose.bigasr.TimedUtterance('UNAPPROVED REPLACEMENT',0,1000),))
        master_transcriptions.persist(self.database,'J',1,self.active,approved);self.database.commit()
        self.service.apply_visual_recut('J',1,self.command(self.plan()))
        stored=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]);before=deepcopy(stored)
        rows=self.database.execute('SELECT * FROM shots').fetchall()
        with patch.object(s6_compose,'ASSETS_DIR',self.root/'data'/'assets'), \
             patch.object(master_transcriptions,'load_matching',side_effect=[approved,approved,replacement]), \
             patch.object(s6_compose.ffmpeg,'probe_duration',new=AsyncMock(return_value=4)), \
             patch.object(s6_compose.bigasr,'transcribe_detailed',side_effect=AssertionError('New ASR POST forbidden')) as asr:
            with self.assertRaises(visual_evidence.ContentReviewRequired):
                asyncio.run(s6_compose._refresh_director_speech_timeline('J',stored,rows,self.database))
            asr.assert_not_called()
        self.assertEqual(stored,before)
        self.assertEqual(json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]),before)

    def queued_alignment(self, with_speech=False):
        self.prepare()
        if with_speech:
            self.sb['masters'][0]['type']='dialogue';self.sb['shots'][0]['subtitle']='Approved'
            spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]);spec['runtime_storyboard']=deepcopy(self.sb)
            self.database.execute('UPDATE jobs SET production_spec_json=?',(json.dumps(spec),));self.save()
            approved=s6_compose.bigasr.Transcription('Approved',0,1000,(s6_compose.bigasr.TimedUtterance('Approved',0,1000),))
            master_transcriptions.persist(self.database,'J',1,self.active,approved);self.database.commit()
        self.service.apply_visual_recut('J',1,self.command(self.plan()))
        node_state_machine.transition_node(self.database,'J','local_compose','start',event_id='fingerprint-align:start')
        node_state_machine.transition_node(self.database,'J','local_compose','require_review',event_id='fingerprint-align:review')
        self.database.execute("UPDATE production_nodes SET error_code='content_review_required',next_action='review_visual_content' WHERE node_id='local_compose'")
        production_projection.persist(self.database,'J');self.database.commit()
        plan=self.plan();self.service.align_cached_visual_timeline('J',1,{**plan['guards'],'source':'active','confirmed':True})
        return json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])

    def test_cached_alignment_rejects_replacement_at_exact_asr_consumption(self):
        from factory import visual_repair, production_compiler
        approved_live=self.queued_alignment(with_speech=True)
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0])
        execution=visual_repair.adapter_storyboard(self.database,'J',production_compiler.ensure_director_timeline(spec['runtime_storyboard']),'frozen')
        original=deepcopy(execution);rows=self.database.execute('SELECT * FROM shots').fetchall()
        approved=s6_compose.bigasr.Transcription('Approved',0,1000,(s6_compose.bigasr.TimedUtterance('Approved',0,1000),))
        replacement=s6_compose.bigasr.Transcription('UNAPPROVED REPLACEMENT',0,1000,(s6_compose.bigasr.TimedUtterance('UNAPPROVED REPLACEMENT',0,1000),))
        with patch.object(s6_compose,'ASSETS_DIR',self.root/'data'/'assets'), \
             patch.object(master_transcriptions,'load_matching',side_effect=[approved,approved,replacement]), \
             patch.object(s6_compose.ffmpeg,'probe_duration',new=AsyncMock(return_value=4)), \
             patch.object(s6_compose.bigasr,'transcribe_detailed',side_effect=AssertionError('New ASR POST forbidden')) as asr:
            with self.assertRaises(visual_evidence.ContentReviewRequired):
                asyncio.run(s6_compose._refresh_director_speech_timeline('J',execution,rows,self.database))
            asr.assert_not_called()
        self.assertEqual(execution,original)
        self.assertEqual(json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]),approved_live)

    def test_queued_alignment_does_not_overwrite_later_picture_or_hook_edits(self):
        approved=self.queued_alignment()
        self.database.execute("UPDATE jobs SET final_path='existing-final'");self.database.commit()
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
        for field in ['video_start','hook']:
            with self.subTest(field=field):
                changed=deepcopy(approved)
                if field=='video_start':changed['shots'][0]['video_start']=.75
                else:changed['hook']='New approved operator hook'
                self.database.execute('UPDATE jobs SET storyboard=?',(json.dumps(changed),));self.database.commit()
                with patch.object(s6_compose,'run',new=AsyncMock()) as run:
                    with self.assertRaises(visual_evidence.ContentReviewRequired):asyncio.run(production_adapters.run_local_compose_node(self.database,node))
                    run.assert_not_awaited()
                self.assertEqual(json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]),changed)

    def test_cached_alignment_rechecks_live_edit_after_adapter_before_actual_s6(self):
        from factory import visual_repair, production_compiler
        approved=self.queued_alignment()
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0])
        frozen=production_compiler.ensure_director_timeline(spec['runtime_storyboard'])
        execution=visual_repair.adapter_storyboard(self.database,'J',frozen,'frozen')
        changed=deepcopy(approved);changed['shots'][0]['video_start']=.75;changed['hook']='Changed after adapter'
        self.database.execute('UPDATE jobs SET storyboard=?',(json.dumps(changed),));self.database.commit()
        with patch.object(s6_compose,'ASSETS_DIR',self.root/'data'/'assets'), \
             patch.object(s6_compose,'_refresh_director_speech_timeline',new=AsyncMock()) as refresh:
            with self.assertRaises(visual_evidence.ContentReviewRequired):asyncio.run(s6_compose._run('J',self.database,storyboard_override=execution))
            refresh.assert_not_awaited()
        self.assertEqual(json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]),changed)

    def test_later_edit_requires_fresh_confirmation_of_displayed_frozen_baseline(self):
        from factory import visual_repair, production_compiler
        approved=self.queued_alignment();changed=deepcopy(approved)
        changed['shots'][0]['video_start']=.75;changed['hook']='New operator hook'
        self.database.execute('UPDATE jobs SET storyboard=?',(json.dumps(changed),));self.database.commit()
        node_state_machine.transition_node(self.database,'J','local_compose','start',event_id='fresh-align:start')
        node_state_machine.transition_node(self.database,'J','local_compose','require_review',event_id='fresh-align:review')
        self.database.execute("UPDATE production_nodes SET error_code='content_review_required',next_action='review_visual_content' WHERE node_id='local_compose'")
        production_projection.persist(self.database,'J');self.database.commit()
        fresh=self.plan();self.assertTrue(fresh['local_alignment_recovery']['executable_now'])
        self.assertEqual(fresh['local_alignment_recovery']['current_hook'],'New operator hook')
        self.assertIsNone(fresh['local_alignment_recovery']['alignment_baseline']['hook'])
        self.service.align_cached_visual_timeline('J',1,{**fresh['guards'],'source':'active','confirmed':True})
        queued=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
        marker=queued[visual_repair.ALIGNMENT_KEY]
        self.assertEqual(marker['confirmed_live_edit_plan_fingerprint'],fresh['guards']['expected_edit_plan_fingerprint'])
        self.assertEqual(marker['frozen_alignment_edit_plan_fingerprint'],fresh['guards']['expected_frozen_alignment_edit_plan_fingerprint'])
        self.assertEqual(queued['hook'],'New operator hook');self.assertEqual(queued['shots'][0]['video_start'],.75)
        self.assertTrue(queued['local_visual_edit_history'][-1]['prior_alignment_authorization'])
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0])
        execution=visual_repair.adapter_storyboard(self.database,'J',production_compiler.ensure_director_timeline(spec['runtime_storyboard']),'frozen')
        self.assertEqual(content_review.edit_plan_fingerprint(execution),marker['frozen_alignment_edit_plan_fingerprint'])

    def test_cached_alignment_rechecks_frozen_input_identity_after_confirmation(self):
        approved=self.queued_alignment()
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]);spec['runtime_storyboard']['hook']='Changed frozen alignment input'
        self.database.execute('UPDATE jobs SET production_spec_json=?',(json.dumps(spec),));self.database.commit()
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
        with patch.object(s6_compose,'run',new=AsyncMock()) as run:
            with self.assertRaises(visual_evidence.ContentReviewRequired):asyncio.run(production_adapters.run_local_compose_node(self.database,node))
            run.assert_not_awaited()
        self.assertEqual(json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]),approved)

    def assert_post_validation_edit_preserved(self, authorization):
        from factory import visual_repair
        if authorization=='alignment':
            approved=self.queued_alignment(with_speech=True)
            verifier_name='verify_alignment'
        else:
            self.prepare();self.sb['shots'][0]['subtitle']='Approved';self.save()
            cached=s6_compose.bigasr.Transcription('Approved',0,1000,(s6_compose.bigasr.TimedUtterance('Approved',0,1000),))
            master_transcriptions.persist(self.database,'J',1,self.active,cached);self.database.commit()
            self.service.apply_visual_recut('J',1,self.command(self.plan()))
            approved=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
            verifier_name='verify_overlay'
        changed=deepcopy(approved);changed['shots'][0]['video_start']=.75;changed['hook']='AFTER S6 VALIDATION'
        original=getattr(visual_repair,verifier_name);calls=[]
        def verify_then_edit(*args,**kwargs):
            result=original(*args,**kwargs);calls.append(True)
            if len(calls)==3:
                self.database.execute('UPDATE jobs SET storyboard=?',(json.dumps(changed),));self.database.commit()
            return result
        class RenderConnection:
            def __getattr__(_self,name):return getattr(self.database,name)
            def close(_self):pass
        async def cut(_picture,_audio,output,**_kwargs):Path(output).write_bytes(b'cut')
        async def mix(_concat,_bgm,output,**_kwargs):Path(output).write_bytes(b'final-if-overwritten')
        node=self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
        with patch.object(visual_repair,verifier_name,side_effect=verify_then_edit), \
             patch.object(s6_compose,'ASSETS_DIR',self.root/'data'/'assets'), \
             patch.object(s6_compose,'conn',return_value=RenderConnection()), \
             patch.object(s6_compose.ffmpeg,'probe_duration',new=AsyncMock(return_value=4)), \
             patch.object(s6_compose.ffmpeg,'cut_director_segment',new=AsyncMock(side_effect=cut)), \
             patch.object(s6_compose.ffmpeg,'concat_shots',new=AsyncMock()), \
             patch.object(s6_compose.ffmpeg,'make_cta_card',new=AsyncMock()), \
             patch.object(s6_compose.ffmpeg,'mix_bgm',new=AsyncMock(side_effect=mix)), \
             patch.object(s6_compose.bigasr,'transcribe_detailed',side_effect=AssertionError('New ASR POST forbidden')) as asr, \
             patch.dict('os.environ',{'GEN_THUMBNAIL':'false','GEN_MARKETING_THUMBNAIL':'false'}):
            with self.assertRaises(visual_evidence.ContentReviewRequired):asyncio.run(production_adapters.run_local_compose_node(self.database,node))
            asr.assert_not_called()
        self.assertEqual(len(calls),3)
        self.assertEqual(json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0]),changed)

    def test_alignment_edit_after_third_validation_is_not_adopted_as_persist_identity(self):
        self.assert_post_validation_edit_preserved('alignment')

    def test_recut_edit_after_third_validation_is_not_adopted_as_persist_identity(self):
        self.assert_post_validation_edit_preserved('recut')

    def test_stale_conflict_is_review_first_and_provider_junk_does_not_crash_routing(self):
        self.prepare()
        qa={'status':'passed','diagnostic':{'visual_evidence':{'provenance':'automatic','asset_sha256':'old-source',
            'requirement_set_hash':'old-requirements','items':[{'id':'hand','status':'conflicting'}]}}}
        self.database.execute('UPDATE shots SET video_qa=?',(json.dumps(qa),));self.database.commit()
        plan=self.plan();self.assertEqual(plan['recommended_route'],'manual_review')
        self.assertEqual(plan['routes'][0]['reason'],'missing_or_stale_evidence_review_first')
        qa['diagnostic']['visual_evidence']='provider junk'
        self.database.execute('UPDATE shots SET video_qa=?',(json.dumps(qa),));self.database.commit()
        self.assertEqual(self.plan()['recommended_route'],'manual_review')

    def test_cannot_relabel_frozen_onscreen_picture_as_offscreen_to_authorize_recut(self):
        self.prepare()
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0])
        spec['runtime_storyboard']['shots'][0]['speech_mode']='on_screen'
        self.database.execute('UPDATE jobs SET production_spec_json=?',(json.dumps(spec),));self.database.commit()
        self.assertFalse(self.plan()['routes'][0]['executable_now'])

    def test_terminated_jobs_never_revive_even_for_duplicate_recut(self):
        self.prepare();command=self.command(self.plan())
        self.service.apply_visual_recut('J',1,command)
        for state in ['PUBLISHED','REJECTED','REJECTED_PENDING_RECONCILIATION']:
            self.database.execute('UPDATE jobs SET state=?',(state,));self.database.commit()
            before='\n'.join(self.database.iterdump())
            with self.assertRaisesRegex(ValueError,'terminated'): self.service.apply_visual_recut('J',1,command)
            self.assertEqual(before,'\n'.join(self.database.iterdump()))

    def test_node_executes_rendered_repair_button_and_explicit_local_dispatch(self):
        self.prepare(); plan=self.plan()
        self.service.apply_visual_recut('J',1,self.command(plan))
        node_state_machine.transition_node(self.database,'J','local_compose','start',event_id='ui-alignment:start')
        node_state_machine.transition_node(self.database,'J','local_compose','require_review',event_id='ui-alignment:review')
        self.database.execute("UPDATE production_nodes SET error_code='content_review_required',next_action='review_visual_content' WHERE node_id='local_compose'")
        production_projection.persist(self.database,'J');self.database.commit()
        realignment=self.plan()
        source=(Path(__file__).resolve().parents[1]/'scripts'/'management_ui'/'app.js').read_text(encoding='utf-8')
        ui=source.split('async function openVisualContentReview',1)[1].split('const openCandidateWithContentBase',1)[0]
        setup=r"""
        const elements=new Map(),toasts=[],posts=[];let markup='';
        function $(selector){if(!elements.has(selector))elements.set(selector,{value:selector==='#content-sha'?'sha':'req',listeners:{},insertAdjacentHTML(_where,html){markup+=html},addEventListener(type,handler){this.listeners[type]=handler}});return elements.get(selector)}
        function $$(selector){return []}function esc(value){return String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')}
        function toast(value){toasts.push(value)}function openModal(_type,title,html){markup+=title+html}function closeModal(){}async function loadPage(){}
        async function api(url,options){if(options){posts.push({url,body:JSON.parse(options.body)});return {already_queued:false}}if(url.includes('/visual-repair'))return repair;return {requirements:[{id:'hand',description:'Raise <hand> & finish'}],duration_sec:4,automatic_findings:{items:[{id:'hand',status:'conflicting',observed_description:'<Observed> & actual'}]},preview_url:'/preview?a=1&b=2',asset_sha256:'hidden-sha',requirement_set_hash:'hidden-req'}}
        """
        script=setup+'\nlet repair='+json.dumps(plan,ensure_ascii=False)+';const realignment='+json.dumps(realignment,ensure_ascii=False)+';\nasync function openVisualContentReview'+ui+r"""
        (async()=>{const shot={idx:1,has_visual_requirements:true},job={job_id:'J',shots:[shot]};await openVisualContentReview(job,shot,'active');await elements.get('#preview-free-recut').onclick();await elements.get('#confirm-free-recut').onclick();repair=realignment;await openVisualContentReview(job,shot,'active');await elements.get('#preview-cached-alignment').onclick();await elements.get('#confirm-cached-alignment').onclick();process.stdout.write(JSON.stringify({markup,toasts,posts}))})();
        """
        result=subprocess.run(['node','-e',script],capture_output=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr);output=json.loads(result.stdout)
        for label in ['修复路线建议','本地画面重剪','确认免费画面重剪','当前画面','新画面','输出保留','确认画面改动并恢复本地合成']:
            self.assertIn(label,output['markup'])
        self.assertIn('Raise &lt;hand&gt; &amp; finish',output['markup'])
        self.assertIn('&lt;Observed&gt; &amp; actual',output['markup'])
        self.assertEqual(len(output['posts']),2)
        self.assertEqual(output['posts'][0]['url'],'/api/videos/J/masters/1/local-recut')
        self.assertEqual(output['posts'][0]['body'],self.command(plan))
        self.assertEqual(output['posts'][1]['url'],'/api/videos/J/masters/1/align-local-timeline')
        self.assertEqual(output['posts'][1]['body'],{**realignment['guards'],'source':'active','confirmed':True})
        self.assertIn('放弃上次已批准的画面重剪',output['markup'])
        self.assertNotIn('confirm-native-edit',output['markup'])

    def test_stale_commands_do_not_mutate_or_queue(self):
        self.prepare(); command = self.command(self.plan())
        for key in ['expected_asset_sha256', 'expected_requirement_set_hash', 'expected_edit_plan_fingerprint', 'expected_spec_hash', 'expected_contract_hash', 'expected_source_bindings']:
            with self.subTest(key=key):
                bad = deepcopy(command); bad[key] = 'changed'
                before = '\n'.join(self.database.iterdump())
                with self.assertRaisesRegex(ValueError, 'stale'): self.service.apply_visual_recut('J', 1, bad)
                self.assertEqual(before, '\n'.join(self.database.iterdump()))

    def test_invalid_and_unauthorized_local_edits_are_rejected(self):
        self.prepare(); command = self.command(self.plan())
        for change in [{'edit_idx':1,'video_start':float('nan')}, {'edit_idx':1,'video_start':float('inf')},
                       {'edit_idx':1,'video_start':4}, {'edit_idx':1,'video_start':-1},
                       {'edit_idx':1,'video_start':True}, {'edit_idx':99,'video_start':1},
                       {'edit_idx':1,'video_start':1,'audio_start':1}, {'edit_idx':1,'video_start':1,'speech_mode':'off_screen'}]:
            with self.subTest(change=change):
                bad = deepcopy(command); bad['edits'] = [change]
                before = '\n'.join(self.database.iterdump())
                with self.assertRaises(ValueError): self.service.apply_visual_recut('J', 1, bad)
                self.assertEqual(before, '\n'.join(self.database.iterdump()))
        bad = deepcopy(command); bad['confirmed'] = False
        with self.assertRaisesRegex(ValueError, 'explicit'): self.service.apply_visual_recut('J', 1, bad)

    def test_complete_action_cannot_be_cropped_by_explicit_edit(self):
        self.prepare(); command = self.command(self.plan()); command['edits'][0]['video_start'] = 2
        with self.assertRaisesRegex(ValueError, 'retain all'): self.service.apply_visual_recut('J', 1, command)
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='local_compose'").fetchone()[0], 'REVIEW_REQUIRED')

    def test_action_spanning_two_edits_requires_ordered_source_continuity(self):
        self.prepare()
        self.sb['shots']=[{**self.sb['shots'][0],'video_start':3,'duration':1},
                         {**self.sb['shots'][0],'idx':2,'video_start':2,'audio_start':1,'duration':1}]
        spec=json.loads(self.database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]);spec['runtime_storyboard']=deepcopy(self.sb)
        self.database.execute('UPDATE jobs SET production_spec_json=?',(json.dumps(spec),));self.save()
        observation=self.payload('active');observation['items'][0].update(start_sec=1,end_sec=3)
        self.service.record_content_review('J',1,observation)
        plan=self.plan();self.assertTrue(plan['routes'][0]['executable_now'])
        bad=self.command(plan);bad['edits']=[{'edit_idx':1,'video_start':2},{'edit_idx':2,'video_start':1}]
        with self.assertRaisesRegex(ValueError,'retain all'):self.service.apply_visual_recut('J',1,bad)
        self.service.apply_visual_recut('J',1,self.command(plan))
        stored=json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
        self.assertEqual([e['video_start'] for e in stored['shots']],[1,2])
        self.assertEqual([e['audio_start'] for e in stored['shots']],[0,1])

    def test_local_window_and_fade_combination_is_not_mislabeled_as_paid_shortage(self):
        self.prepare()
        self.sb['shots']=[{**self.sb['shots'][0],'video_start':3,'duration':1},
                         {**self.sb['shots'][0],'idx':2,'video_start':2,'audio_start':1,'duration':1,
                          'transition_type':'dip_to_black','transition_duration_sec':.2}]
        self.save();observation=self.payload('active');observation['items'][0].update(start_sec=1,end_sec=3)
        self.service.record_content_review('J',1,observation)
        plan=self.plan();self.assertEqual(plan['recommended_route'],'local_recut')
        self.assertEqual(plan['routes'][0]['reason'],'local_transition_adjustment_required')
        self.assertFalse(plan['routes'][0]['executable_now'])

    def test_onscreen_unknown_and_mouth_defects_never_get_free_recut(self):
        self.prepare()
        for change in [{'speech_mode':'on_screen'}, {'speech_mode':None}, {'requires_lip_sync':True}]:
            with self.subTest(change=change):
                self.sb['shots'][0].update(change); self.save()
                self.assertFalse(self.plan()['routes'][0]['executable_now'])
        self.sb['shots'][0]['speech_mode'] = 'off_screen'; self.sb['shots'][0].pop('requires_lip_sync'); self.save()
        qa = json.loads(self.database.execute('SELECT video_qa FROM shots').fetchone()[0]); qa['reasons'] = ['lip_sync_mismatch']
        self.database.execute('UPDATE shots SET video_qa=?', (json.dumps(qa),)); self.database.commit()
        self.assertFalse(self.plan()['routes'][0]['executable_now'])
        self.assertEqual(self.plan()['defects'][0]['bound_to_selected'], False)

    def test_missing_conflicting_waived_and_candidate_evidence_routes_are_truthful(self):
        self.prepare()
        for status in ['insufficient_evidence', 'conflicting', 'not_applicable']:
            with self.subTest(status=status):
                self.service.record_content_review('J', 1, self.payload('active',status))
                plan = self.plan()
                self.assertFalse(plan['routes'][0]['executable_now'])
                if status == 'conflicting': self.assertEqual(plan['recommended_route'], 'bounded_native_edit')
                if status == 'not_applicable':
                    self.assertEqual(plan['waivers'][0]['status'], 'not_applicable')
                    self.assertEqual(plan['recommended_route'], 'manual_review')
        qa = json.loads(self.database.execute('SELECT video_qa FROM shots').fetchone()[0]); qa['candidate_path'] = str(self.candidate)
        self.database.execute('UPDATE shots SET video_qa=?',(json.dumps(qa),)); self.database.commit()
        self.service.record_content_review('J', 1, self.payload('candidate'))
        plan = self.service.visual_repair_plan('J', 1, 'candidate')
        self.assertEqual(plan['routes'][0]['reason'], 'accept_current_candidate_first')
        self.assertFalse(plan['routes'][0]['executable_now'])

    def test_pending_paid_tasks_prefer_get_reconcile_and_preserve_ledger(self):
        self.prepare(); command = self.command(self.plan())
        self.database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,cost_cents,usage_json) VALUES('paid-pending','J',1,'seedance',999,?)", (json.dumps({'status':'pending'}),)); self.database.commit()
        before = '\n'.join(self.database.iterdump()); plan = self.plan()
        self.assertEqual(plan['recommended_route'], 'manual_review')
        self.assertEqual(plan['unresolved_provider_task_ids'], ['paid-pending'])
        with self.assertRaisesRegex(ValueError, 'paid-pending'): self.service.apply_visual_recut('J', 1, command)
        self.assertEqual(before, '\n'.join(self.database.iterdump()))

    def test_fade_only_problem_is_a_local_transition_recommendation(self):
        self.prepare()
        self.sb['shots'] = [{**self.sb['shots'][0], 'video_start':0, 'duration':.5},
            {**self.sb['shots'][0], 'idx':2, 'video_start':1, 'audio_start':.5, 'duration':1,
             'transition_type':'dip_to_black', 'transition_duration_sec':.2}]
        self.save(); plan = self.plan()
        self.assertEqual(plan['routes'][0]['reason'], 'local_transition_adjustment_required')
        self.assertEqual(plan['recommended_route'], 'local_recut')
        self.assertFalse(plan['routes'][0]['executable_now'])

    def test_complete_event_source_window_shortage_is_distinct_from_conflict(self):
        self.prepare(); self.sb['shots'][0]['duration'] = .5; self.save()
        plan = self.plan()
        self.assertEqual(plan['routes'][0]['reason'], 'source_window_insufficient_for_complete_action')
        self.assertEqual(plan['recommended_route'], 'extend')
        self.assertIsNone(plan['routes'][1]['cost']['estimated_cost_cents'])
        self.assertFalse(plan['routes'][1]['implemented'])
        self.assertFalse(plan['routes'][2]['executable_now'])

    def test_repeat_restart_and_caller_rollback_are_idempotent_and_owned(self):
        from factory.management_api import ManagementService
        self.prepare(); command = self.command(self.plan()); before = '\n'.join(self.database.iterdump())
        with patch('factory.management_api.subprocess.Popen',side_effect=AssertionError('Caller transaction must not launch a worker')) as worker:
            self.database.execute('BEGIN')
            result=self.service.apply_visual_recut('J', 1, command)
            self.assertTrue(result['dispatch_deferred']);self.assertTrue(self.database.in_transaction)
            self.assertTrue(self.service.dispatch_production_queue()['dispatch_deferred'])
            worker.assert_not_called();self.database.rollback()
        self.assertEqual(before, '\n'.join(self.database.iterdump()))
        self.service.apply_visual_recut('J', 1, command)
        restarted = ManagementService(self.database, self.root)
        self.assertTrue(restarted.apply_visual_recut('J', 1, command)['already_queued'])
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM production_node_events WHERE event='resume_visual_content'").fetchone()[0], 1)

    def test_own_command_commits_before_the_supervisor_launches_local_worker(self):
        self.prepare();result=self.service.apply_visual_recut('J',1,self.command(self.plan()))
        self.assertFalse(result['dispatch_deferred']);self.assertFalse(self.database.in_transaction)
        def launch(*_args,**_kwargs):
            self.assertFalse(self.database.in_transaction)
            return Mock(pid=12345,poll=Mock(return_value=None))
        with patch('factory.management_api.subprocess.Popen',side_effect=launch) as worker:
            dispatched=self.service.dispatch_production_queue()
            worker.assert_called_once();self.assertEqual(dispatched['dispatched_jobs'],['J'])

    def test_discard_alignment_caller_transaction_defers_worker_and_rolls_back_audit(self):
        self.prepare();self.service.apply_visual_recut('J',1,self.command(self.plan()))
        node_state_machine.transition_node(self.database,'J','local_compose','start',event_id='caller-align:start')
        node_state_machine.transition_node(self.database,'J','local_compose','require_review',event_id='caller-align:review')
        self.database.execute("UPDATE production_nodes SET error_code='content_review_required',next_action='review_visual_content' WHERE node_id='local_compose'")
        production_projection.persist(self.database,'J');self.database.commit()
        plan=self.plan();before='\n'.join(self.database.iterdump())
        with patch('factory.management_api.subprocess.Popen',side_effect=AssertionError('Caller transaction must not launch a worker')) as worker:
            self.database.execute('BEGIN')
            result=self.service.align_cached_visual_timeline('J',1,{**plan['guards'],'source':'active','confirmed':True})
            self.assertTrue(result['dispatch_deferred']);self.assertTrue(self.service.dispatch_production_queue()['dispatch_deferred'])
            worker.assert_not_called();self.database.rollback()
        self.assertEqual(before,'\n'.join(self.database.iterdump()))

    def test_final_approval_is_invalidated_without_touching_paid_material(self):
        self.prepare(); command = self.command(self.plan())
        self.database.execute("UPDATE jobs SET state='READY_TO_PUBLISH',cost_cents=777")
        self.database.execute("UPDATE production_nodes SET state='SUCCEEDED',error_code=NULL WHERE node_id='local_compose'")
        self.database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,idempotency_key) VALUES('J','final_review','final_review','SUCCEEDED','frozen','final')")
        self.database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,cost_cents) VALUES('paid-original','J',1,'seedance',777)"); self.database.commit()
        paid = [tuple(r) for r in self.database.execute('SELECT * FROM provider_calls')]
        self.service.apply_visual_recut('J', 1, command)
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='final_review'").fetchone()[0], 'PENDING')
        self.assertEqual(self.database.execute("SELECT state FROM production_nodes WHERE node_id='master:1'").fetchone()[0], 'SUCCEEDED')
        self.assertEqual([tuple(r) for r in self.database.execute('SELECT * FROM provider_calls')], paid)
        job = self.database.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(job['cost_cents'],777); self.assertEqual(job['bgm_path'],str(self.bgm))

    def test_changed_source_contract_spec_or_plan_pauses_overlay_before_render(self):
        self.prepare(); self.service.apply_visual_recut('J', 1, self.command(self.plan()))
        node = self.database.execute("SELECT * FROM production_nodes WHERE node_id='local_compose'").fetchone()
        stored = json.loads(self.database.execute('SELECT storyboard FROM jobs').fetchone()[0])
        for mutation in ['source', 'contract', 'plan', 'path']:
            with self.subTest(mutation=mutation):
                altered = deepcopy(stored)
                if mutation == 'source': self.active.write_bytes(b'changed')
                if mutation == 'contract':
                    altered['visual_requirements'][0]['description']='Changed action'
                    altered['masters'][0]['visual_requirements']=deepcopy(altered['visual_requirements'])
                if mutation == 'plan': altered['shots'][0]['audio_start']=.2
                if mutation == 'path':
                    outside = self.root/'outside.mp4'; outside.write_bytes(b'prior')
                    self.database.execute('UPDATE shots SET video_path=?',(str(outside),))
                self.database.execute('UPDATE jobs SET storyboard=?',(json.dumps(altered),)); self.database.commit()
                with patch.object(s6_compose,'run',new=AsyncMock()) as run:
                    with self.assertRaises(visual_evidence.ContentReviewRequired): asyncio.run(production_adapters.run_local_compose_node(self.database,node))
                    run.assert_not_awaited()
                self.active.write_bytes(b'prior')
                self.database.execute('UPDATE shots SET video_path=?',(str(self.active),)); self.database.commit()
        self.database.execute("UPDATE jobs SET storyboard=?,production_spec_hash='new-spec'",(json.dumps(stored),)); self.database.commit()
        with self.assertRaises(production_adapters.FrozenSpecMismatchError): asyncio.run(production_adapters.run_local_compose_node(self.database,node))

    def test_http_get_plan_and_explicit_post_use_real_service(self):
        from management_server import ConsoleHandler
        self.prepare()
        class Handler(ConsoleHandler): pass
        Handler.service = self.service
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        url=f'http://127.0.0.1:{server.server_port}/api/videos/J/masters/1'
        try:
            before='\n'.join(self.database.iterdump())
            with request.urlopen(url+'/visual-repair?source=active') as response: plan=json.load(response)
            self.assertEqual(before,'\n'.join(self.database.iterdump()))
            bad=self.command(plan);bad['confirmed']=False
            with self.assertRaises(error.HTTPError): request.urlopen(request.Request(url+'/local-recut',json.dumps(bad).encode(),{'Content-Type':'application/json'},method='POST'))
            with request.urlopen(request.Request(url+'/local-recut',json.dumps(self.command(plan)).encode(),{'Content-Type':'application/json'},method='POST')) as response:
                self.assertTrue(json.load(response)['local_only'])
            node_state_machine.transition_node(self.database,'J','local_compose','start',event_id='http-alignment:start')
            node_state_machine.transition_node(self.database,'J','local_compose','require_review',event_id='http-alignment:review')
            self.database.execute("UPDATE production_nodes SET error_code='content_review_required',next_action='review_visual_content' WHERE node_id='local_compose'")
            production_projection.persist(self.database,'J');self.database.commit()
            with request.urlopen(url+'/visual-repair?source=active') as response:alignment=json.load(response)
            align={**alignment['guards'],'source':'active','confirmed':True}
            with request.urlopen(request.Request(url+'/align-local-timeline',json.dumps(align).encode(),{'Content-Type':'application/json'},method='POST')) as response:
                result=json.load(response);self.assertTrue(result['local_only']);self.assertTrue(result['alignment_only'])
        finally: server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':
    unittest.main()
