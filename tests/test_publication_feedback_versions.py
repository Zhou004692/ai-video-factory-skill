"""Measured metrics and immutable publication history contracts."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory import db, performance_feedback as feedback, creative_learning


class PublicationFeedbackVersionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'factory.db'
        with patch.dict('os.environ', {'FACTORY_DB_PATH': str(self.path)}):
            self.db = db.conn()
        self.db.execute("INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J','T','Title','PUBLISHED')")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def snapshot(self, **values):
        return feedback.record_snapshot(self.db, 'J', platform='douyin',
            captured_at='2026-10-13T00:00:00Z', **values)

    def test_missing_is_unknown_and_explicit_zero_is_measured(self):
        self.snapshot(views=100, likes=0)
        row = feedback._latest_snapshots(self.db)[0]
        self.assertIsNone(row['comments'])
        self.assertEqual(row['likes'], 0)
        self.assertIsNone(feedback._aggregate([row])['engagement_rate'])
        self.assertIsNone(feedback._aggregate([row])['completion_rate'])

    def test_caller_metadata_cannot_forge_provided_or_creative_context(self):
        self.snapshot(views=100, metadata={'provided_fields': ['comments'],
            'provenance': 'measured', 'creative_context': {'tags': {'hook_type': 'fake'}}})
        row = feedback._latest_snapshots(self.db)[0]
        self.assertIsNone(row['comments'])
        metadata = json.loads(row['metadata_json'])
        self.assertNotEqual(metadata.get('provided_fields'), ['comments'])
        self.assertNotEqual(metadata.get('creative_context'), {'tags': {'hook_type': 'fake'}})

    def test_count_types_and_nonfinite_watch_are_rejected(self):
        for value in (True, 1.5, float('nan'), float('inf'), -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.snapshot(likes=value)
        for value in (True, float('nan'), float('inf'), -1):
            with self.subTest(watch=value), self.assertRaises(ValueError):
                self.snapshot(average_watch_sec=value)

    def test_platform_and_window_validation(self):
        with self.assertRaises(ValueError):
            feedback.record_snapshot(self.db, 'J', platform='fake', views=10)
        with self.assertRaises(ValueError):
            self.snapshot(metadata={'observation_window_hours': -2})

    def test_publication_timestamp_rejects_boolean(self):
        with self.assertRaises(ValueError):
            feedback.register_publication(self.db,'J',platform='douyin',item_id='bad-time',published_at=False)

    def test_same_item_idempotent_new_item_history_and_same_time_feedback(self):
        first = feedback.register_publication(self.db, 'J', platform='douyin', item_id='one')
        a = self.snapshot(publication_id=first, views=100)
        self.assertEqual(first, feedback.register_publication(self.db, 'J', platform='douyin', item_id='one'))
        second = feedback.register_publication(self.db, 'J', platform='douyin', item_id='two')
        b = self.snapshot(publication_id=second, views=200)
        self.assertNotEqual(first, second)
        self.assertNotEqual(a, b)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM platform_publications').fetchone()[0], 2)
        self.assertEqual(len(feedback._latest_snapshots(self.db)), 2)

    def test_existing_item_feedback_survives_new_edit_and_missing_final(self):
        publication = feedback.register_publication(self.db, 'J', platform='douyin', item_id='one')
        self.db.execute("UPDATE jobs SET state='COMPOSED',final_path=NULL,publish_url=NULL WHERE job_id='J'")
        self.db.commit()
        self.snapshot(publication_id=publication, views=10)
        self.assertEqual(feedback._latest_snapshots(self.db)[0]['views'], 10)

    def test_pending_platform_item_does_not_accept_published_performance(self):
        publication = feedback.register_publication(self.db,'J',platform='douyin',item_id='pending',metadata={'review_status':'pending'})
        with self.assertRaisesRegex(ValueError,'pending'):
            self.snapshot(publication_id=publication,views=10)

    def test_replace_at_same_instant_clears_omitted_counts(self):
        original = self.snapshot(views=100, likes=10)
        repeated = self.snapshot(views=200)
        self.assertEqual(original, repeated)
        self.assertIsNone(feedback._latest_snapshots(self.db)[0]['likes'])

    def test_individual_metric_denominators(self):
        self.snapshot(views=100, completion_rate=0.4, follows=10)
        feedback.record_snapshot(self.db, 'J', platform='weixin_channels', views=900,
            captured_at='2026-10-13T00:00:00Z')
        result = feedback._aggregate(feedback._latest_snapshots(self.db))
        self.assertEqual(result['completion_rate'], 0.4)
        self.assertEqual(result['follow_rate'], 0.1)
        self.assertEqual(result['coverage']['follow_rate']['views'], 100)

    def test_strategy_unknowns_remain_unknown(self):
        result = creative_learning._aggregate_creatives([
            {'views': 100, 'completion_rate': None, 'engagement_rate': None,
             'follow_rate': None, 'lead_rate': None}])
        self.assertIsNone(result['completion_rate'])

    def service(self):
        from factory.management_api import ManagementService
        return ManagementService(self.db, Path(self.temp.name), data_dir=Path(self.temp.name), worker_mode='external')

    def test_management_records_missing_counts_and_historical_item_independently(self):
        service = self.service()
        first = service.register_publication({'job_id':'J','platform':'douyin','item_id':'one'})['publication_id']
        second = service.register_publication({'job_id':'J','platform':'douyin','item_id':'two'})['publication_id']
        service.record_performance_snapshot(first, {'views':100,'likes':0})
        service.record_performance_snapshot(second, {'views':200})
        rows = {r['publication_id']:r for r in service.publishing()['publications']}
        self.assertEqual(rows[first]['latest_snapshot']['views'],100)
        self.assertIsNone(rows[first]['latest_snapshot']['comments'])
        self.assertEqual(rows[second]['latest_snapshot']['views'],200)
        self.assertNotEqual(first,second)

    def test_cli_and_csv_preserve_omitted_counts(self):
        import cli
        from click.testing import CliRunner
        with patch.object(cli,'conn',return_value=self.db):
            result = CliRunner().invoke(cli.cli,['performance-add','J','--platform','douyin','--views','100'])
        self.assertEqual(result.exit_code,0,result.output)
        self.assertIsNone(feedback._latest_snapshots(self.db)[0]['likes'])
        path = Path(self.temp.name)/'feedback.csv'
        path.write_text('job_id,platform,captured_at,views,likes\nJ,weixin_channels,2026-10-13T01:00:00Z,100,0\n',encoding='utf-8')
        feedback.import_csv(self.db,path)
        row = feedback._latest_snapshots(self.db,platform='weixin_channels')[0]
        self.assertEqual(row['likes'],0)
        self.assertIsNone(row['comments'])

    def test_publication_context_captures_asset_and_tags_without_rebinding(self):
        assets = Path(self.temp.name)/'assets'/'J'
        assets.mkdir(parents=True)
        final = assets/'final.mp4'
        final.write_bytes(b'first-published-bytes')
        self.db.execute('UPDATE jobs SET final_path=? WHERE job_id=?',(str(final),'J'))
        brief = {'recommended_index':0,'angles':[{'title':'Original'}],'source_score':{'overall':80}}
        self.db.execute("INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,approved_job_id) VALUES('B','approved','Original',?,'J')",(json.dumps(brief),))
        creative_learning.tag_angle(self.db,'B',angle_index=0,hook_type='pain_point',narrative_pattern='case_story',proof_type='visual_demo',cta_type='follow')
        frozen = {'title':'Original','script_metadata':{'title':'Original','creative_thesis':'intent A',
            'creative_tags':{'hook_type':'pain_point','narrative_pattern':'case_story','proof_type':'visual_demo','cta_type':'follow'}}}
        self.db.execute('UPDATE jobs SET production_spec_json=? WHERE job_id=?',
            (json.dumps({'runtime_storyboard':frozen,'creative_ir_hash':'frozen-A'}),'J'))
        self.db.commit()
        creative_learning.tag_angle(self.db,'B',angle_index=0,hook_type='question',narrative_pattern='comparison',proof_type='none',cta_type='none')
        pub = self.service().register_publication({'job_id':'J','platform':'douyin','item_id':'one','metadata':{'creative_context':{'fake':True}}})['publication_id']
        original = self.db.execute('SELECT context_json FROM platform_publications WHERE publication_id=?',(pub,)).fetchone()[0]
        self.assertIsNotNone(json.loads(original)['final_sha256'])
        self.assertEqual(json.loads(original)['intent'],'intent A')
        creative_learning.tag_angle(self.db,'B',angle_index=0,hook_type='question',narrative_pattern='comparison',proof_type='none',cta_type='none')
        final.unlink()
        self.db.execute("UPDATE jobs SET state='COMPOSED' WHERE job_id='J'")
        self.db.commit()
        self.service().record_performance_snapshot(pub,{'views':100,'completion_rate':0.5})
        creative = feedback.learning_report(self.db)['creatives'][0]
        self.assertEqual(creative['tags']['hook_type'],'pain_point')
        self.assertEqual(self.db.execute('SELECT context_json FROM platform_publications WHERE publication_id=?',(pub,)).fetchone()[0],original)

    def test_legacy_migration_preserves_ids_raw_values_and_unknowns(self):
        self.db.close()
        # A genuine old layout, independent of new DDL.
        self.path.unlink()
        old = sqlite3.connect(self.path)
        old.executescript("""CREATE TABLE jobs(job_id TEXT PRIMARY KEY,topic_id TEXT,title TEXT,state TEXT);
        INSERT INTO jobs VALUES('J','T','Old','PUBLISHED');
        CREATE TABLE platform_publications(publication_id TEXT PRIMARY KEY,job_id TEXT,platform TEXT,item_id TEXT,item_url TEXT,title TEXT,published_at TEXT,metadata_json TEXT,created_at TEXT,updated_at TEXT,UNIQUE(platform,item_id),UNIQUE(job_id,platform));
        INSERT INTO platform_publications VALUES('OLD-PUB','J','douyin','old',NULL,'Old','2026-10-12T00:00:00Z','{}','created','updated');
        CREATE TABLE performance_snapshots(snapshot_id TEXT PRIMARY KEY,job_id TEXT,platform TEXT,captured_at TEXT,views INTEGER NOT NULL DEFAULT 0,completed_views INTEGER,completion_rate REAL,average_watch_sec REAL,likes INTEGER NOT NULL DEFAULT 0,comments INTEGER NOT NULL DEFAULT 0,shares INTEGER NOT NULL DEFAULT 0,saves INTEGER NOT NULL DEFAULT 0,follows INTEGER NOT NULL DEFAULT 0,leads INTEGER NOT NULL DEFAULT 0,metadata_json TEXT,created_at TEXT,updated_at TEXT,UNIQUE(job_id,platform,captured_at));
        INSERT INTO performance_snapshots VALUES('OLD-PERF','J','douyin','2026-10-13T00:00:00+00:00',100,NULL,NULL,NULL,10,0,0,0,0,0,'{"raw":"retained"}','created','updated');""")
        old.close()
        # jobs are old too; restore only absent base columns so this specifically tests feedback migration.
        with patch.dict('os.environ',{'FACTORY_DB_PATH':str(self.path)}):
            self.db = db.conn()
        row = self.db.execute("SELECT * FROM performance_snapshots WHERE snapshot_id='OLD-PERF'").fetchone()
        self.assertEqual(row['comments'],0)
        self.assertEqual(row['metadata_json'],'{"raw":"retained"}')
        projected = feedback.project_snapshot(row)
        self.assertIsNone(projected['comments'])
        self.assertEqual(projected['likes'],10)
        self.assertEqual(projected['publication_id'],'OLD-PUB')
        self.assertEqual(projected['creative_context'],{})
        self.db.close()
        with patch.dict('os.environ',{'FACTORY_DB_PATH':str(self.path)}):
            self.db = db.conn()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM performance_snapshots').fetchone()[0],1)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def experiment_fixture(self):
        from test_creative_experiments import CreativeExperimentTests
        fixture = CreativeExperimentTests()
        fixture.memory_db = self.db
        experiment = fixture._create_experiment()
        from factory import creative_experiments
        jobs = []
        for arm,hook in [('control','pain_point'),('treatment','contrarian')]:
            for i in range(2):
                job = fixture._add_job(f'{arm}{i}',hook_type=hook)
                creative_experiments.assign_job(self.db,experiment,job,arm=arm)
                jobs.append(job)
        return experiment,jobs

    def frozen_publication(self, job, item, tags):
        final = Path(self.temp.name)/'assets'/job/'final.mp4'
        final.parent.mkdir(parents=True,exist_ok=True)
        final.write_bytes(item.encode())
        frozen = {'title':item,'production_profile':'standard',
                  'script_metadata':{'title':item,'creative_tags':tags}}
        self.db.execute('UPDATE jobs SET final_path=?,production_spec_json=? WHERE job_id=?',
            (str(final),json.dumps({'runtime_storyboard':frozen}),job))
        self.db.commit()
        return self.service().register_publication({'job_id':job,'platform':'douyin',
            'item_id':item,'published_at':'2026-08-12T00:00:00Z'})['publication_id']

    def test_experiment_rejects_unknown_or_conflicting_publication_tags(self):
        from factory import creative_experiments
        experiment,jobs = self.experiment_fixture()
        tag_sets = [
            {'hook_type':'question','narrative_pattern':'problem_solution','proof_type':'source_quote','cta_type':'consult'},
            {},
            {'hook_type':'contrarian','narrative_pattern':'comparison','proof_type':'source_quote','cta_type':'consult'},
            {'hook_type':'contrarian'},
        ]
        for i,job in enumerate(jobs):
            publication = self.frozen_publication(job,f'conflict-{job}',tag_sets[i])
            feedback.record_snapshot(self.db,job,platform='douyin',publication_id=publication,
                views=100,completion_rate=0.1 if i<2 else 0.9,captured_at='2026-08-12T22:00:00Z')
        result = creative_experiments.experiment_report(self.db,experiment)
        self.assertEqual(result['directional_assessment'],'insufficient_data')
        self.assertEqual(result['arms']['control']['sample_count'],0)
        self.assertEqual(result['arms']['treatment']['sample_count'],0)
        reasons = {item['job_id']:item for item in result['excluded']}
        self.assertEqual(reasons[jobs[0]]['reason'],'publication_creative_tags_mismatch')
        self.assertEqual(reasons[jobs[1]]['reason'],'publication_creative_tags_unknown')
        self.assertEqual(reasons[jobs[2]]['conflicting_dimensions'],['narrative_pattern'])
        self.assertIn('proof_type',reasons[jobs[3]]['unknown_dimensions'])

    def test_experiment_matching_frozen_tags_survive_mutable_tag_changes(self):
        from factory import creative_experiments
        experiment,jobs = self.experiment_fixture()
        for i,job in enumerate(jobs):
            tags = {'hook_type':'pain_point' if i<2 else 'contrarian',
                'narrative_pattern':'problem_solution','proof_type':'source_quote','cta_type':'consult'}
            publication = self.frozen_publication(job,f'matching-{job}',tags)
            creative_learning.tag_angle(self.db,f'BRIEF-EXP-{"control" if i<2 else "treatment"}{i%2}',
                angle_index=0,hook_type='question',narrative_pattern='comparison',proof_type='none',cta_type='none')
            feedback.record_snapshot(self.db,job,platform='douyin',publication_id=publication,
                views=100,completion_rate=0.1 if i<2 else 0.9,captured_at='2026-08-12T22:00:00Z')
        result = creative_experiments.experiment_report(self.db,experiment)
        self.assertEqual(result['directional_assessment'],'treatment_candidate')
        self.assertEqual(result['arms']['control']['sample_count'],2)
        self.assertEqual(result['arms']['treatment']['sample_count'],2)
        self.assertEqual(result['excluded'],[])

    def test_partial_frozen_tags_have_independent_dimension_coverage(self):
        publication = self.frozen_publication('J','partial-tags',{'hook_type':'question'})
        feedback.record_snapshot(self.db,'J',platform='douyin',publication_id=publication,
            views=100,completion_rate=0.6,captured_at='2026-08-12T22:00:00Z')
        result = creative_learning.strategy_report(self.db,platform='douyin',window_hours=24,min_samples=1)
        dimensions = {row['dimension']:row for row in result['dimensions']}
        self.assertEqual(dimensions['hook_type']['eligible'][0]['value'],'question')
        self.assertEqual(dimensions['hook_type']['coverage']['known_samples'],1)
        for dimension in ('narrative_pattern','proof_type','cta_type'):
            self.assertEqual(dimensions[dimension]['eligible'],[])
            self.assertEqual(dimensions[dimension]['coverage']['unknown_samples'],1)
            self.assertEqual(dimensions[dimension]['unknown'][0]['publication_id'],publication)

    def test_unverified_current_association_is_preserved_without_creative_attribution(self):
        brief = {'recommended_index':0,'source_score':{'overall':90},'angles':[{'title':'Mutable draft'}]}
        self.db.execute("INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,approved_job_id,approved_angle_index) VALUES('B','approved','Mutable draft',?,'J',0)",(json.dumps(brief),))
        self.db.execute("INSERT INTO script_sources(source_id,fingerprint,source_type,title,content) VALUES('S','fp','text','Raw source','Actual source text')")
        self.db.execute("INSERT INTO topic_brief_sources(brief_id,source_id) VALUES('B','S')")
        self.db.commit()
        creative_learning.tag_angle(self.db,'B',angle_index=0,hook_type='question',narrative_pattern='comparison',proof_type='none',cta_type='follow')
        service = self.service()
        publication = service.register_publication({'job_id':'J','platform':'douyin','item_id':'declaration','published_at':'2026-08-12T00:00:00Z'})['publication_id']
        service.record_performance_snapshot(publication,{'views':100,'completion_rate':0.9,'captured_at':'2026-08-12T22:00:00Z'})
        context = json.loads(self.db.execute('SELECT context_json FROM platform_publications').fetchone()[0])
        self.assertEqual(context['tags'],{})
        self.assertEqual(context['tags_provenance'],'unknown')
        self.assertEqual(context['declared_association']['tags']['hook_type'],'question')
        report = feedback.learning_report(self.db,platform='douyin',window_hours=24)
        self.assertEqual(report['creatives'],[])
        self.assertEqual(report['sources'],[])
        self.assertEqual(report['score_bands'],[])
        self.assertEqual(report['observations'][0]['views'],100)
        self.assertEqual(report['observations'][0]['completion_rate'],0.9)
        self.assertEqual(self.db.execute("SELECT content FROM script_sources WHERE source_id='S'").fetchone()[0],'Actual source text')
        self.assertTrue(all(not row['eligible'] for row in creative_learning.strategy_report(self.db,platform='douyin',window_hours=24,min_samples=1)['dimensions']))

    def test_persisted_unverified_tags_are_not_promoted_by_report(self):
        publication = feedback.register_publication(self.db,'J',platform='douyin',item_id='stored-bad',published_at='2026-08-12T00:00:00Z')
        bad = {'brief_id':'B','creative_provenance':'unverified_current_association','tags_provenance':'unverified_current_association',
            'creative_ir':{'title':'Mutable draft'},'tags':{'hook_type':'question','narrative_pattern':'comparison','proof_type':'none','cta_type':'follow'},
            'sources':[{'source_id':'S','title':'Mutable source'}],'source_score':90}
        original = json.dumps(bad)
        self.db.execute('UPDATE platform_publications SET context_json=? WHERE publication_id=?',(original,publication))
        self.db.commit()
        self.snapshot(publication_id=publication,views=100,completion_rate=0.9)
        report = feedback.learning_report(self.db)
        self.assertEqual(report['creatives'],[])
        self.assertEqual(report['sources'],[])
        self.assertEqual(report['score_bands'],[])
        self.assertEqual(report['observations'][0]['views'],100)
        self.assertEqual(self.db.execute('SELECT context_json FROM platform_publications').fetchone()[0],original)
        # Defense at the strategy boundary even when a caller supplies a pre-existing read model.
        with patch.object(feedback,'learning_report',return_value={'creatives':[{'job_id':'J','publication_id':publication,
            'views':100,'completion_rate':0.9,'tags':bad['tags'],'creative_context':bad}],
            'excluded':[],'platform':'douyin','window_hours':24}):
            strategy = creative_learning.strategy_report(self.db,platform='douyin',window_hours=24,min_samples=1)
        self.assertTrue(all(not row['eligible'] for row in strategy['dimensions']))

    def test_frozen_manual_association_keeps_intent_observations_without_upload_claim(self):
        frozen = {'script_metadata':{'title':'Frozen intent','creative_tags':{'hook_type':'question'},'source_ids':['S']}}
        self.db.execute('UPDATE jobs SET production_spec_json=? WHERE job_id=?',(json.dumps({'runtime_storyboard':frozen}),'J'))
        self.db.commit()
        service = self.service()
        publication = service.register_publication({'job_id':'J','platform':'douyin','item_id':'manual-frozen','published_at':'2026-08-12T00:00:00Z'})['publication_id']
        service.record_performance_snapshot(publication,{'views':100,'completion_rate':0.6,'captured_at':'2026-08-12T22:00:00Z'})
        context = json.loads(self.db.execute('SELECT context_json FROM platform_publications').fetchone()[0])
        self.assertNotEqual(context['binding_status'],'server_verified_douyin_upload')
        strategy = creative_learning.strategy_report(self.db,platform='douyin',window_hours=24,min_samples=1)
        self.assertEqual(strategy['dimensions'][0]['eligible'][0]['value'],'question')
        self.assertEqual(feedback.learning_report(self.db)['sources'][0]['source_id'],'S')

    def test_experiment_missing_primary_metrics_never_selects_winner(self):
        from factory import creative_experiments
        experiment,jobs = self.experiment_fixture()
        for job in jobs:
            feedback.record_snapshot(self.db,job,platform='douyin',views=100,
                captured_at='2026-08-12T23:00:00Z')
        result = creative_experiments.experiment_report(self.db,experiment)
        self.assertEqual(result['directional_assessment'],'insufficient_data')
        self.assertIsNone(result['absolute_delta'])

    def test_experiment_old_item_cannot_replace_current_item_or_double_sample(self):
        from factory import creative_experiments
        experiment,jobs = self.experiment_fixture()
        for job in jobs:
            old = self.db.execute('SELECT publication_id FROM platform_publications WHERE job_id=?',(job,)).fetchone()[0]
            current = feedback.register_publication(self.db,job,platform='douyin',item_id=f'new-{job}',published_at='2026-08-12T00:00:00Z')
            feedback.record_snapshot(self.db,job,platform='douyin',publication_id=current,views=100,completion_rate=0.4,captured_at='2026-08-12T22:00:00Z')
            feedback.record_snapshot(self.db,job,platform='douyin',publication_id=old,views=900,completion_rate=0.9,captured_at='2026-08-12T23:00:00Z')
        result = creative_experiments.experiment_report(self.db,experiment)
        self.assertEqual(result['arms']['control']['sample_count'],2)
        self.assertEqual(result['arms']['control']['views'],200)

    def test_remeasured_legacy_row_returns_persisted_id(self):
        self.db.execute("INSERT INTO performance_snapshots(snapshot_id,job_id,platform,captured_at,views) VALUES('legacy-id','J','douyin','2026-10-13T00:00:00+00:00',10)")
        self.db.commit()
        measured = self.snapshot(views=100)
        self.assertEqual(measured,'legacy-id')

    def test_false_time_and_inconsistent_window_are_rejected(self):
        for values in ({'captured_at':False}, {'captured_at':'2026-10-13T00:00:00Z','metadata':{'observation_window_hours':24,
                'observation_start_at':'2026-10-12T22:00:00Z',
                'observation_end_at':'2026-10-12T23:00:00Z'}}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                feedback.record_snapshot(self.db,'J',platform='douyin',views=10,**values)

    def test_real_http_json_boundary_preserves_unknown_and_rejects_invalid(self):
        from http.server import ThreadingHTTPServer
        from management_server import ConsoleHandler
        from urllib import request,error
        import threading
        service = self.service()
        publication = service.register_publication({'job_id':'J','platform':'douyin','item_id':'http'})['publication_id']
        handler = type('FeedbackHandler',(ConsoleHandler,),{'service':service,'auth_manager':None})
        server = ThreadingHTTPServer(('127.0.0.1',0),handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def post(payload):
            return request.urlopen(request.Request(f'http://127.0.0.1:{server.server_port}/api/publications/{publication}/performance',
                data=json.dumps(payload).encode(),method='POST',headers={'Content-Type':'application/json'}),timeout=5)
        try:
            with post({'views':100,'likes':0,'comments':''}) as response:
                result = json.load(response)['latest_snapshot']
            self.assertEqual(result['likes'],0)
            self.assertIsNone(result['comments'])
            self.assertEqual(result['publication_id'],publication)
            for invalid in ({'views':True},{'shares':1.5},{'average_watch_sec':float('inf')},
                            {'captured_at':'2026-10-13T01:00:00'}, {'metadata':{'observation_window_hours':-1}}):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(error.HTTPError) as raised:
                        post(invalid)
                    self.assertEqual(raised.exception.code,400)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_actual_cli_process_can_select_archived_work_without_provider_access(self):
        import os,subprocess,sys
        publication = feedback.register_publication(self.db,'J',platform='douyin',item_id='cli-old')
        feedback.register_publication(self.db,'J',platform='douyin',item_id='cli-new')
        self.db.execute("UPDATE jobs SET state='COMPOSED',final_path=NULL WHERE job_id='J'")
        self.db.commit()
        env = {key:value for key,value in os.environ.items() if not key.startswith(('ARK_','VOLC_','TOS_','ANTHROPIC_','SUNO_','DOUYIN_','FACTORY_'))}
        env.update(FACTORY_DB_PATH=str(self.path),FACTORY_ENV_FILE=str(Path(self.temp.name)/'absent.env'),PYTHONUTF8='1')
        script = Path(__file__).resolve().parents[1]/'scripts'/'cli.py'
        # Actual CLI __main__ entry, inherited disposable runtime, all sockets blocked.
        wrapper = "import socket,runpy,sys; socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(OSError('offline CLI')); sys.path.insert(0,str(__import__('pathlib').Path(sys.argv[1]).parent)); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
        args = [sys.executable,'-c',wrapper,str(script),'performance-add','J','--platform','douyin','--publication-id',publication,
                '--captured-at','2026-10-13T00:00:00Z','--views','100','--likes','0']
        result = subprocess.run(args,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        row = feedback._latest_snapshots(self.db)[0]
        self.assertEqual(row['publication_id'],publication)
        self.assertEqual(row['likes'],0)
        self.assertIsNone(row['comments'])
        invalid = subprocess.run(args+['--shares','1.5'],env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertNotEqual(invalid.returncode,0)
