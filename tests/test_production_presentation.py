import unittest


class PresentationTests(unittest.TestCase):
    def present(self, **overrides):
        from factory.services.production_presentation import present_production
        evidence = {'state': 'STORYBOARDED', 'nodes': [], 'shots': [],
                    'provider_calls': [], 'plan_passed': 3, 'plan_total': 3,
                    'allowed_actions': [], 'next_action': None}
        evidence.update(overrides)
        return present_production(evidence)

    def test_plan_passed_is_not_production_ready(self):
        result = self.present()
        self.assertEqual(result['plan'], {'passed': 3, 'total': 3})
        self.assertEqual(result['production']['completed'], 0)
        self.assertFalse(result['gate']['can_advance'])
        self.assertEqual(result['activity']['running'], 0)

    def test_pending_nodes_in_paused_job_are_not_running(self):
        result = self.present(state='FAILED', nodes=[{'node_id': 'master:1',
            'node_type': 'master', 'state': 'PENDING'}])
        self.assertEqual(result['activity']['running'], 0)
        self.assertEqual(result['activity']['queued'], 0)

    def test_legacy_queue_without_execution_evidence_is_unknown(self):
        result = self.present(state='PRODUCTION_QUEUED', execution_engine='legacy')
        self.assertEqual(result['activity']['queued'], 0)
        self.assertEqual(result['activity']['running'], 0)
        self.assertEqual(result['activity']['unknown'], 1)
        self.assertFalse(result['submission_known'])

    def test_remote_evidence_survives_parent_pause(self):
        result = self.present(state='FAILED', provider_calls=[{'task_id': 'remote-1',
            'usage_json': '{"status":"accepted"}'}])
        self.assertEqual(result['activity']['remote_accepted'], 1)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('处理', result['label'])

    def test_historic_account_failure_does_not_override_policy_node(self):
        result = self.present(state='ACCOUNT_ACTION_REQUIRED', failure_code='account_required',
            nodes=[{'node_id': 'master:1', 'node_type': 'master', 'state': 'FAILED',
                    'error_code': 'output_policy_rejected'}])
        self.assertEqual(result['error_summary']['category'], 'content_policy')

    def test_completed_node_without_media_is_blocked(self):
        result = self.present(nodes=[{'node_id': 'master:1', 'node_type': 'master',
            'state': 'SUCCEEDED'}], shots=[{'idx': 1, 'state': 'OK', 'media_exists': False}])
        self.assertFalse(result['gate']['can_advance'])
        self.assertEqual(result['production']['completed'], 0)

    def test_lapsed_node_lease_is_unknown_not_running(self):
        result = self.present(state='FAILED', nodes=[{'node_id': 'master:1',
            'node_type': 'master', 'state': 'RUNNING', 'lease_valid': False}])
        self.assertEqual(result['activity']['running'], 0)
        self.assertEqual(result['activity']['unknown'], 1)
        self.assertFalse(result['submission_known'])

    def test_available_media_still_requires_content_and_anchor_acceptance(self):
        result = self.present(state='FAILED', nodes=[{'node_id': 'master:1',
            'node_type': 'master', 'state': 'SUCCEEDED'}],
            shots=[{'idx': 1, 'state': 'OK', 'media_exists': True}],
            anchor_blocked=True, content_blocked=True)
        self.assertEqual(result['production'], {'completed': 1, 'total': 1})
        self.assertFalse(result['gate']['can_advance'])
        self.assertTrue({'anchor_review_required', 'content_review_required'} <=
                        {b['code'] for b in result['gate']['blockers']})

    def test_compiled_graph_is_not_submission_acceptance(self):
        result = self.present(nodes=[{'node_id': 'master:1', 'node_type': 'master', 'state': 'READY'}])
        self.assertFalse(result['submission_known'])
        self.assertIsNone(result['submission_evidence']['receipt_id'])

    def test_receipt_identity_stable_across_reads_and_changes_only_with_evidence(self):
        receipt = {'id': 'J:queued:operation-1', 'kind': 'queue_transition', 'event': 'queued',
                   'created_at': '2026-10-13 10:00:00', 'target': {'job_id': 'J'}}
        first = self.present(state='PRODUCTION_QUEUED', submission_receipts=[receipt], observed_at='first')
        second = self.present(state='PRODUCTION_QUEUED', submission_receipts=[receipt], observed_at='second')
        changed = self.present(state='PRODUCTION_QUEUED', submission_receipts=[{**receipt, 'id': 'J:queued:operation-2'}])
        self.assertTrue(first['submission_known'])
        self.assertEqual(first['submission_evidence']['receipt_id'], second['submission_evidence']['receipt_id'])
        self.assertNotEqual(first['submission_evidence']['receipt_id'], changed['submission_evidence']['receipt_id'])

    def test_finishing_existing_provider_task_does_not_change_receipt_identity(self):
        pending = self.present(state='FAILED', provider_calls=[{'task_id': 'old-task', 'usage_json': '{"status":"pending"}'}])
        completed = self.present(state='FAILED', provider_calls=[{'task_id': 'old-task', 'usage_json': '{"status":"completed"}'}])
        self.assertIsNotNone(pending['submission_evidence']['receipt_id'])
        self.assertEqual(pending['submission_evidence']['receipt_id'], completed['submission_evidence']['receipt_id'])

    def test_conflicting_current_errors_require_verification(self):
        result = self.present(state='FAILED', nodes=[
            {'node_id': 'master:1', 'state': 'FAILED', 'error_code': 'output_policy_rejected'},
            {'node_id': 'master:2', 'state': 'FAILED', 'error_code': 'account_unavailable'}])
        self.assertEqual(result['error_summary']['category'], 'awaiting_verification')
        self.assertTrue(result['error_summary']['conflicting_evidence'])


class PresentationReadIntegrationTests(unittest.TestCase):
    def test_seen_revision_is_required_for_approval_and_brief_confirmation(self):
        from test_management_console import ManagementServiceTests
        from unittest.mock import patch
        fixture = ManagementServiceTests('test_candidate_script_can_be_edited_and_locks_after_approval')
        fixture.setUp()
        try:
            fixture._insert_editable_content(approved=True)
            fixture.database.execute("UPDATE script_drafts SET revision=2 WHERE draft_id='SCRIPT-EDIT'")
            fixture.database.execute("UPDATE topic_briefs SET revision=2 WHERE brief_id='BRIEF-EDIT'")
            fixture.database.commit()
            before = fixture.database.serialize()
            with self.assertRaises(ValueError) as conflict:
                fixture.service.approve_script('SCRIPT-EDIT', expected_revision=1)
            self.assertEqual(conflict.exception.payload['code'], 'script_revision_conflict')
            with patch('factory.management_api.script_drafts.generate_drafts') as generate:
                with self.assertRaises(ValueError) as conflict:
                    fixture.service.confirm_brief_and_generate_scripts('BRIEF-EDIT', angle_index=0, expected_revision=1)
                self.assertEqual(conflict.exception.payload['code'], 'brief_revision_conflict')
                generate.assert_not_called()
            self.assertEqual(fixture.database.serialize(), before)
            result = fixture.service.approve_script('SCRIPT-EDIT', expected_revision=2)
            self.assertTrue(result['job_id'])
            self.assertFalse(fixture.database.in_transaction)
            with patch('factory.management_api.script_drafts.generate_drafts', return_value={'drafts': [], 'cached': True}) as generate:
                result = fixture.service.confirm_brief_and_generate_scripts('BRIEF-EDIT', angle_index=0, expected_revision=2)
                self.assertTrue(result['job_id'])
                generate.assert_called_once()
                self.assertFalse(fixture.database.in_transaction)
        finally:
            fixture.tearDown()
            fixture.doCleanups()

    def test_compile_returns_fresh_confirmation_job_with_matching_spec(self):
        from test_production_service import ProductionServiceTests
        from factory.management_api import ManagementService
        fixture = ProductionServiceTests('test_compile_assign_and_read_graph_stay_inside_production_boundary')
        fixture.setUp()
        try:
            service = ManagementService(fixture.database, fixture.root, worker_mode='external', env_path=fixture.root / '.env')
            fixture.database.execute("UPDATE jobs SET title='fresh quote' WHERE job_id='J-SERVICE'")
            fixture.database.commit()
            compiled = service.compile_video_job('J-SERVICE')
            self.assertTrue(compiled['compilable'])
            self.assertEqual(compiled['confirmation_job']['title'], 'fresh quote')
            self.assertEqual(compiled['confirmation_job']['production_spec_hash'], compiled['spec_hash'])
            self.assertIn('preflight', compiled['confirmation_job'])
            self.assertFalse(fixture.database.in_transaction)
        finally:
            fixture.tearDown()
            fixture.doCleanups()

    def test_seen_candidate_and_final_hash_reject_replaced_media_without_mutation(self):
        from test_content_review import ContentReviewTests
        import hashlib
        fixture = ContentReviewTests('test_generic_candidate_accept_cannot_bypass_content')
        fixture.setUp()
        try:
            old_candidate = hashlib.sha256(fixture.candidate.read_bytes()).hexdigest()
            fixture.candidate.write_bytes(b'replacement candidate')
            before = fixture.database.serialize()
            with self.assertRaises(ValueError) as conflict:
                fixture.service.review_master_candidate('J', 1, decision='accept_candidate', expected_candidate_sha256=old_candidate)
            self.assertEqual(conflict.exception.payload['code'], 'candidate_media_conflict')
            self.assertEqual(fixture.database.serialize(), before)
            final = fixture.base / 'final.mp4'
            final.write_bytes(b'old final')
            old_final = hashlib.sha256(final.read_bytes()).hexdigest()
            fixture.database.execute("UPDATE jobs SET state='COMPOSED',final_path=? WHERE job_id='J'", (str(final),))
            fixture.database.commit()
            final.write_bytes(b'replacement final')
            before = fixture.database.serialize()
            with self.assertRaises(ValueError) as conflict:
                fixture.service.review_video('J', decision='approved', checks={}, expected_final_sha256=old_final)
            self.assertEqual(conflict.exception.payload['code'], 'final_media_conflict')
            self.assertEqual(fixture.database.serialize(), before)
            self.assertEqual(fixture.candidate.read_bytes(), b'replacement candidate')
            self.assertEqual(final.read_bytes(), b'replacement final')
        finally:
            fixture.tearDown()

    def test_publishing_receipt_identity_is_read_only_and_lifecycle_stable(self):
        from test_management_http_concurrency import HttpConcurrencyTests
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = HttpConcurrencyTests().make_service(directory)
            try:
                database.execute("INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J','T','publish','READY_TO_PUBLISH')")
                database.execute("INSERT INTO platform_publications(publication_id,job_id,platform,item_id) VALUES('OLD','J','douyin','old-item')")
                database.commit()
                before = database.serialize()
                empty = service.publishing()['submission_evidence']['J']
                self.assertIsNone(empty['receipt_id'])
                self.assertFalse(empty['submission_known'])
                self.assertEqual(database.serialize(), before)
                database.execute("INSERT INTO platform_submissions(submission_id,job_id,platform,state) VALUES('SUB','J','douyin','uploading')")
                database.commit()
                uploading = service.publishing()['submission_evidence']['J']
                self.assertFalse(uploading['submission_known'])
                database.execute("UPDATE platform_submissions SET state='published',item_id='new-item' WHERE submission_id='SUB'")
                database.commit()
                before = database.serialize()
                completed = service.publishing()['submission_evidence']['J']
                self.assertTrue(completed['submission_known'])
                self.assertEqual(uploading['receipt_id'], completed['receipt_id'])
                self.assertEqual(database.serialize(), before)
            finally:
                database.close()

    def test_stale_brief_revision_keeps_history_and_candidate_drafts_unchanged(self):
        from test_management_console import ManagementServiceTests
        fixture = ManagementServiceTests('test_candidate_brief_can_be_edited_with_revision_history')
        fixture.setUp()
        try:
            fixture._insert_editable_content()
            fixture.database.execute("UPDATE topic_briefs SET revision=2 WHERE brief_id='BRIEF-EDIT'")
            fixture.database.commit()
            before = fixture.database.serialize()
            with self.assertRaisesRegex(ValueError, '版本'):
                fixture.service.update_brief('BRIEF-EDIT', fixture._editable_brief_payload(title='stale'), expected_revision=1)
            self.assertEqual(fixture.database.serialize(), before)
            result = fixture.service.update_brief('BRIEF-EDIT', fixture._editable_brief_payload(title='current'), expected_revision=2)
            self.assertEqual(result['revision'], 3)
        finally:
            fixture.tearDown()
            fixture.doCleanups()

    def test_source_performance_digest_rejects_old_analysis_and_accepts_current(self):
        from test_management_http_concurrency import HttpConcurrencyTests
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = HttpConcurrencyTests().make_service(directory)
            try:
                analysis = {'segments': [{'start_sec': 0, 'end_sec': 2, 'speaker': 'unknown', 'dialogue': 'old'}]}
                database.execute("INSERT INTO script_sources(source_id,fingerprint,source_type,title,content,metadata_json) VALUES('SRC','fp','video','test','body',?)", (json.dumps({'structure_analysis': analysis}),))
                database.commit()
                old_digest = service.scripts()['sources'][0]['analysis_digest']
                analysis['segments'][0]['dialogue'] = 'reanalyzed segment'
                database.execute("UPDATE script_sources SET metadata_json=? WHERE source_id='SRC'", (json.dumps({'structure_analysis': analysis}),))
                database.commit()
                before = database.serialize()
                payload = {'expected_analysis_digest': old_digest, 'assignments': [{'segment_index': 0, 'speaker': 'host', 'speech_mode': 'off_screen'}]}
                with self.assertRaisesRegex(ValueError, '解析已变化'):
                    service.review_source_performance('SRC', payload)
                self.assertEqual(database.serialize(), before)
                current = service.scripts()['sources'][0]['analysis_digest']
                result = service.review_source_performance('SRC', {**payload, 'expected_analysis_digest': current})
                self.assertEqual(result['analysis']['segments'][0]['dialogue'], 'reanalyzed segment')
                self.assertEqual(result['analysis']['segments'][0]['speaker'], 'host')
                self.assertNotEqual(result['analysis_digest'], current)
                self.assertFalse(database.in_transaction)
            finally:
                database.close()

    def test_publication_history_is_bounded_read_only_and_retains_frozen_context(self):
        from test_management_http_concurrency import HttpConcurrencyTests
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = HttpConcurrencyTests().make_service(directory)
            try:
                database.execute("INSERT INTO jobs(job_id,topic_id,title,state,storyboard) VALUES('J','T','current intent','PUBLISHED',?)", (json.dumps({'intent': 'new intent'}),))
                database.execute("INSERT INTO platform_publications(publication_id,job_id,platform,item_id) VALUES('PUB','J','douyin','item')")
                for index in range(25):
                    database.execute("INSERT INTO performance_snapshots(snapshot_id,job_id,platform,captured_at,publication_id,observation_key,views,measurement_version,metadata_json,context_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (f'S-{index}', 'J', 'douyin', f'2026-10-{index + 1:02d}T00:00:00Z', 'PUB', 'PUB', index, 1,
                         json.dumps({'observation_source': f'captured source {index}'}),
                         json.dumps({'intent': f'old intent {index}', 'final_sha256': f'old-final-{index}'})))
                database.commit()
                before = database.serialize()
                publication = service.publishing()['publications'][0]
                self.assertEqual(database.serialize(), before)
                self.assertEqual(publication['snapshot_history_total'], 25)
                self.assertEqual(len(publication['snapshot_history']), 20)
                latest = publication['snapshot_history'][0]
                self.assertEqual(latest['creative_context']['intent'], 'old intent 24')
                self.assertEqual(latest['creative_context']['final_sha256'], 'old-final-24')
                self.assertEqual(latest['metadata']['observation_source'], 'captured source 24')
            finally:
                database.close()
    def test_current_frozen_spec_queues_after_transaction_commit_without_worker(self):
        from test_production_service import ProductionServiceTests
        from factory.management_api import ManagementService
        fixture = ProductionServiceTests('test_compile_assign_and_read_graph_stay_inside_production_boundary')
        fixture.setUp()
        try:
            service = ManagementService(fixture.database, fixture.root, worker_mode='external', env_path=fixture.root / '.env')
            compilation = service.assign_execution_engine('J-SERVICE', 'graph_v2')
            result = service.start_video_job('J-SERVICE', confirmed=True, approved_budget_cents=10000,
                high_risk_confirmed=True, expected_production_spec_hash=compilation['spec_hash'])
            self.assertEqual(result['status'], 'queued')
            self.assertFalse(fixture.database.in_transaction)
            self.assertEqual(fixture.database.execute("SELECT state FROM jobs WHERE job_id='J-SERVICE'").fetchone()[0], 'PRODUCTION_QUEUED')
            self.assertEqual(fixture.database.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0], 0)
        finally:
            fixture.tearDown()
            fixture.doCleanups()
    def test_stale_production_spec_is_rejected_before_preflight_or_paid_boundary(self):
        from test_management_http_concurrency import HttpConcurrencyTests
        from unittest.mock import patch
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = HttpConcurrencyTests().make_service(directory)
            try:
                database.execute("INSERT INTO jobs(job_id,topic_id,title,state,production_spec_hash) VALUES('J','T','spec guard','STORYBOARDED',?)", ('a' * 64,))
                database.commit()
                before = database.serialize()
                with patch.object(service, '_require_transport_preflight') as preflight, patch.object(service.production_service, 'start') as start:
                    with self.assertRaisesRegex(ValueError, '规格'):
                        service.start_video_job('J', confirmed=True, expected_production_spec_hash='b' * 64)
                    preflight.assert_not_called()
                    start.assert_not_called()
                self.assertEqual(database.serialize(), before)
            finally:
                database.close()

    def test_old_project_uses_full_single_job_dto_outside_recent_eighty(self):
        from test_management_http_concurrency import HttpConcurrencyTests
        from tests.fixtures.management_factory import seed_management_factory
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = HttpConcurrencyTests().make_service(directory)
            try:
                seed_management_factory(database)
                media = Path(directory) / 'data/assets/J-E2E-REVIEW/final.mp4'
                media.parent.mkdir(parents=True)
                media.write_bytes(b'existing accepted local media')
                database.execute("UPDATE jobs SET final_path=? WHERE job_id='J-E2E-REVIEW'", (str(media),))
                database.execute("INSERT INTO review_events(job_id,decision) VALUES('J-E2E-REVIEW','approved')")
                for index in range(81):
                    database.execute("INSERT INTO jobs(job_id,topic_id,title,state,created_at) VALUES(?,?,?,'FAILED','2030-01-01')", (f'J-NEW-{index}', 'T', 'recent'))
                database.commit()
                self.assertNotIn('J-E2E-REVIEW', {j['job_id'] for j in service.videos()['jobs']})
                project = next(p for p in service.content_projects()['projects'] if (p.get('video') or {}).get('job_id') == 'J-E2E-REVIEW')
                self.assertTrue(project['video']['final_exists'])
                self.assertEqual(project['video']['latest_review']['decision'], 'approved')
                self.assertIsInstance(project['video']['next_action'], dict)
                self.assertTrue(project['stage_gates']['review']['can_advance'])
            finally:
                database.close()
    def test_production_health_reads_do_not_initialize_capacity(self):
        from test_management_http_concurrency import HttpConcurrencyTests
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = HttpConcurrencyTests().make_service(directory)
            try:
                before = database.serialize()
                statements = []
                database.set_trace_callback(statements.append)
                with lock:
                    health = service.production_health(supervisor_alive=False)
                self.assertEqual(database.serialize(), before)
                self.assertFalse(any(sql.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'ALTER ')) for sql in statements))
                self.assertGreaterEqual(health['master_capacity']['effective_limit'], 1)
            finally:
                database.close()

    def test_graph_content_review_is_an_actionable_todo_decision(self):
        from test_management_http_concurrency import HttpConcurrencyTests
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = HttpConcurrencyTests().make_service(directory)
            try:
                database.execute("INSERT INTO jobs(job_id,topic_id,title,state,execution_engine) VALUES('J-CONTENT','T','review content','PRODUCTION_REVIEW_REQUIRED','graph_v2')")
                database.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,idempotency_key,error_code) VALUES('J-CONTENT','local_compose','local_compose','REVIEW_REQUIRED','spec','J-CONTENT:local_compose','content_review_required')")
                database.commit()
                todos = service.todo_center()
                decisions = [i for g in todos['groups'] for i in g['items']]
                content = [i for i in decisions if i['kind'] == 'content_review']
                self.assertEqual(len(content), 1)
                self.assertEqual(content[0]['target']['node_id'], 'local_compose')
                self.assertEqual(todos['item_count'], 1)
            finally:
                database.close()
    def test_concurrent_script_change_during_validation_cannot_be_overwritten(self):
        from test_management_console import ManagementServiceTests
        from factory import script_drafts
        from factory.management_api import ManagementService
        from unittest.mock import patch
        import sqlite3
        import json
        fixture = ManagementServiceTests('test_candidate_script_can_be_edited_and_locks_after_approval')
        fixture.setUp()
        local = remote = None
        try:
            fixture._insert_editable_content(approved=True)
            path = fixture.root / 'concurrent-edit.db'
            local = sqlite3.connect(path)
            local.row_factory = sqlite3.Row
            fixture.database.backup(local)
            service = ManagementService(local, fixture.root, env_path=fixture.root / '.env')
            remote = sqlite3.connect(path)
            original = script_drafts._normalize_variant
            remote_payload = fixture._editable_script_payload(title='concurrent winner')
            def interleave(*args, **kwargs):
                normalized = original(*args, **kwargs)
                remote.execute("UPDATE script_drafts SET script_json=?,revision=2 WHERE draft_id='SCRIPT-EDIT'", (json.dumps(remote_payload),))
                remote.commit()
                return normalized
            with patch.object(script_drafts, '_normalize_variant', side_effect=interleave), self.assertRaisesRegex(ValueError, '版本'):
                service.update_script_draft('SCRIPT-EDIT', fixture._editable_script_payload(title='stale loser'), expected_revision=1)
            row = local.execute("SELECT script_json,revision FROM script_drafts WHERE draft_id='SCRIPT-EDIT'").fetchone()
            self.assertEqual(row['revision'], 2)
            self.assertEqual(json.loads(row['script_json'])['title'], 'concurrent winner')
            self.assertEqual(local.execute('SELECT COUNT(*) FROM content_revisions').fetchone()[0], 0)
        finally:
            if remote:
                remote.close()
            if local:
                local.close()
            fixture.tearDown()
            fixture.doCleanups()

    def test_script_edit_rejects_stale_revision_without_changing_content(self):
        from test_management_console import ManagementServiceTests
        fixture = ManagementServiceTests('test_candidate_script_can_be_edited_and_locks_after_approval')
        fixture.setUp()
        try:
            fixture._insert_editable_content(approved=True)
            fixture.database.execute("UPDATE script_drafts SET revision=2 WHERE draft_id='SCRIPT-EDIT'")
            fixture.database.commit()
            before = fixture.database.serialize()
            with self.assertRaisesRegex(ValueError, '版本'):
                fixture.service.update_script_draft('SCRIPT-EDIT', fixture._editable_script_payload(title='stale'), expected_revision=1)
            self.assertEqual(fixture.database.serialize(), before)
        finally:
            fixture.tearDown()
            fixture.doCleanups()

    def test_script_edit_accepts_current_revision_and_preserves_history(self):
        from test_management_console import ManagementServiceTests
        fixture = ManagementServiceTests('test_candidate_script_can_be_edited_and_locks_after_approval')
        fixture.setUp()
        try:
            fixture._insert_editable_content(approved=True)
            result = fixture.service.update_script_draft('SCRIPT-EDIT', fixture._editable_script_payload(title='current'), expected_revision=1)
            self.assertEqual(result['revision'], 2)
            self.assertEqual(result['script']['title'], 'current')
            self.assertEqual(fixture.database.execute("SELECT revision FROM content_revisions WHERE entity_type='script' AND entity_id='SCRIPT-EDIT'").fetchone()[0], 1)
        finally:
            fixture.tearDown()
            fixture.doCleanups()

    def test_reads_are_pure_and_script_variants_count_as_one_decision(self):
        from test_management_http_concurrency import HttpConcurrencyTests
        from tests.fixtures.management_factory import seed_management_factory
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = HttpConcurrencyTests().make_service(directory)
            try:
                seed_management_factory(database)
                before_changes = database.total_changes
                statements = []
                database.set_trace_callback(statements.append)
                with lock:
                    projects = service.content_projects()['projects']
                    videos = service.videos()['jobs']
                    todos = service.todo_center()
                self.assertEqual(database.total_changes, before_changes)
                self.assertFalse(any(sql.lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE ', 'CREATE ', 'ALTER ', 'DROP ')) for sql in statements))
                decisions = [item for group in todos['groups'] for item in group['items']]
                self.assertEqual(todos['item_count'], len(decisions))
                self.assertEqual(len({i['decision_id'] for i in decisions}), len(decisions))
                draft_decision = [i for i in decisions if i['decision_id'] == 'script:BRIEF-E2E-SCRIPT']
                self.assertEqual(len(draft_decision), 1)
                self.assertGreater(len(draft_decision[0]['candidate_ids']), 1)
                self.assertFalse(todos['filter']['truncated'])
                self.assertTrue(all('stage_gates' in p for p in projects))
                self.assertTrue(all(j['presentation']['schema_version'] == 1 for j in videos))
                setup = next(p for p in projects if p['stage'] == 'production_setup')
                self.assertFalse(setup['stage_gates']['video']['can_advance'])
                self.assertEqual(setup['presentation']['production']['completed'], 0)
            finally:
                database.close()
