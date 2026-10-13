"""Real HTTP checks against isolated ephemeral servers, never the live console."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import sqlite3
import json
import time

with patch('dotenv.load_dotenv', return_value=False):
    from management_server import ConsoleHandler


class HttpConcurrencyTests(unittest.TestCase):
    def test_external_io_observations_are_bounded_safe_readonly_and_record_actual_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = self.make_service(directory)
            entered, release = threading.Event(), threading.Event()
            before = database.serialize()
            results = []
            def operation():
                entered.set(); release.wait(5)
                return 'secret operation output is never recorded'
            def run():
                with lock:
                    results.append(service.external_io(operation, step='provider_inspection',
                        target={'job_id': 'J-SAFE', 'task_id': 'remote-1', 'url': 'https://signed.example/?signature=secret'}))
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with lock:
                    pending = service.production_health(supervisor_alive=False)['management_io_observations']
                self.assertEqual(pending, [])
                self.assert_read_responsive(service, lock)
            finally:
                release.set(); worker.join(5)
            with lock:
                observations = service.production_health(supervisor_alive=False)['management_io_observations']
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0]['step'], 'provider_inspection')
            self.assertEqual(observations[0]['target'], {'job_id': 'J-SAFE', 'task_id': 'remote-1'})
            self.assertEqual(observations[0]['outcome'], 'ok')
            self.assertGreater(observations[0]['elapsed_ms'], 200)
            self.assertNotIn('secret', json.dumps(observations))
            self.assertEqual(results, ['secret operation output is never recorded'])
            observations[0]['target']['job_id'] = 'tampered copy'
            with self.assertRaisesRegex(RuntimeError, 'private-url'):
                with lock:
                    service.external_io(lambda: (_ for _ in ()).throw(RuntimeError('private-url https://x/?token=secret')),
                        step='secret invalid step https://x', target={'job_id': 'C:\\Users\\private', 'feed_id': 'F-SAFE', 'error': 'sensitive'})
            with lock:
                after_error = service.production_health(supervisor_alive=False)['management_io_observations']
            self.assertEqual(after_error[0]['target']['job_id'], 'J-SAFE')
            self.assertEqual(after_error[-1]['step'], 'external_io')
            self.assertEqual(after_error[-1]['outcome'], 'error')
            self.assertEqual(after_error[-1]['target'], {'feed_id': 'F-SAFE'})
            self.assertNotIn('secret', json.dumps(after_error))
            self.assertNotIn('private', json.dumps(after_error))
            for _ in range(105):
                with lock:
                    service.external_io(lambda: None)
            with lock:
                observations = service.production_health(supervisor_alive=False)['management_io_observations']
            self.assertEqual(len(observations), 100)
            self.assertNotIn('secret', json.dumps(observations))
            self.assertNotIn('private', json.dumps(observations))
            self.assertEqual(database.serialize(), before)
            self.assertFalse(database.in_transaction)
            database.close()

    def test_verified_final_spills_large_media_and_never_blocks_health_or_lists(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = self.make_service(directory)
            media = Path(directory) / 'data/assets/J/final.mp4'
            media.parent.mkdir(parents=True)
            media.write_bytes(b'x' * (9 * 1024 * 1024))
            digest = hashlib.sha256(media.read_bytes()).hexdigest()
            database.execute("INSERT INTO jobs(job_id,topic_id,title,state,final_path) VALUES('J','T','large versioned file','COMPOSED',?)", (str(media),))
            database.commit()
            handler_done = threading.Event()
            def observed_get(handler):
                try:
                    ConsoleHandler.do_GET(handler)
                finally:
                    if handler.path.startswith('/api/videos/J/file'):
                        handler_done.set()
            handler = type('LargeVersionedHandler', (ConsoleHandler,), {'service': service,
                'service_lock': lock, 'runtime_mode': 'desktop', 'auth_manager': None, 'do_GET': observed_get})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            entered, release = threading.Event(), threading.Event()
            spools, results = [], []
            original_spool = tempfile.SpooledTemporaryFile
            def slow_spool(*args, **kwargs):
                sealed = original_spool(*args, **kwargs)
                spools.append(sealed)
                original_write = sealed.write
                def write(chunk):
                    if not entered.is_set():
                        entered.set(); release.wait(5)
                    return original_write(chunk)
                sealed.write = write
                return sealed
            def download():
                client = HTTPConnection(*server.server_address, timeout=6)
                try:
                    client.request('GET', '/api/videos/J/file?expected_sha256=' + digest, headers={'Range': 'bytes=0-10'})
                    response = client.getresponse()
                    results.append((response.status, response.read()))
                finally:
                    client.close()
            worker = threading.Thread(target=download)
            try:
                with patch('management_server.tempfile.SpooledTemporaryFile', side_effect=slow_spool):
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(database.in_transaction)
                    for path in ('/healthz', '/readyz', '/api/scripts', '/api/todos'):
                        client = HTTPConnection(*server.server_address, timeout=0.7)
                        try:
                            client.request('GET', path)
                            response = client.getresponse()
                            self.assertEqual(response.status, 200, response.read())
                        finally:
                            client.close()
                    release.set(); worker.join(5)
                self.assertEqual(results, [(206, b'x' * 11)])
                self.assertEqual(len(spools), 1)
                self.assertTrue(spools[0]._rolled)
                self.assertTrue(handler_done.wait(2))
                self.assertTrue(spools[0].closed)
            finally:
                release.set(); worker.join(5)
                server.shutdown(); server.server_close(); thread.join(2); database.close()

    def test_final_file_version_guard_streams_verified_spool_and_preserves_ranges(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = self.make_service(directory)
            media = Path(directory) / 'data/assets/J/final.mp4'
            media.parent.mkdir(parents=True)
            media.write_bytes(b'old version')
            old_sha = hashlib.sha256(media.read_bytes()).hexdigest()
            media.write_bytes(b'current verified final bytes')
            current_bytes = media.read_bytes()
            current_sha = hashlib.sha256(current_bytes).hexdigest()
            database.execute("INSERT INTO jobs(job_id,topic_id,title,state,final_path) VALUES('J','T','file version','COMPOSED',?)", (str(media),))
            database.commit()
            class ReplacedAfterHeaders(ConsoleHandler):
                def end_headers(self):
                    if self.headers.get('X-Test-Replace-After-Verification'):
                        media.write_bytes(b'changed after verification')
                    return super().end_headers()
            handler = type('VersionedFileHandler', (ReplacedAfterHeaders,), {'service': service,
                'service_lock': lock, 'runtime_mode': 'desktop', 'auth_manager': None})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                client = HTTPConnection(*server.server_address, timeout=2)
                client.request('GET', '/api/videos/J/file?expected_sha256=' + old_sha)
                response = client.getresponse()
                self.assertEqual(response.status, 409)
                conflict = json.loads(response.read())
                self.assertEqual(conflict['code'], 'final_media_conflict')
                self.assertEqual(conflict['current_final_sha256'], current_sha)
                client.close()
                client = HTTPConnection(*server.server_address, timeout=2)
                client.request('GET', '/api/videos/J/file?download=1&expected_sha256=' + current_sha,
                    headers={'Range': 'bytes=2-8', 'X-Test-Replace-After-Verification': 'true'})
                response = client.getresponse()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.getheader('Content-Range'), f'bytes 2-8/{len(current_bytes)}')
                self.assertEqual(response.getheader('Content-Disposition'), 'attachment; filename="J.mp4"')
                self.assertEqual(response.read(), current_bytes[2:9])
                client.close()
                self.assertEqual(media.read_bytes(), b'changed after verification')
                client = HTTPConnection(*server.server_address, timeout=2)
                client.request('GET', '/api/videos/J/file', headers={'Range': 'bytes=-5'})
                response = client.getresponse()
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b'ation')
                client.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(2); database.close()

    def test_cold_media_hash_runs_without_snapshot_lock_and_discards_replaced_asset(self):
        import factory.management_api as api
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = self.make_service(directory)
            media = Path(directory) / 'data/assets/J/final.mp4'
            media.parent.mkdir(parents=True)
            media.write_bytes(b'first media identity')
            database.execute("INSERT INTO jobs(job_id,topic_id,title,state,final_path) VALUES('J','T','cold hash','COMPOSED',?)", (str(media),))
            database.commit()
            entered, release = threading.Event(), threading.Event()
            original = api._file_sha256
            def slow_hash(path):
                digest = original(path)
                entered.set()
                release.wait(5)
                return digest
            handler = type('ColdHashHandler', (ConsoleHandler,), {'service': service,
                'service_lock': lock, 'runtime_mode': 'desktop', 'auth_manager': None})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            results = []
            def load_videos():
                client = HTTPConnection(*server.server_address, timeout=6)
                try:
                    client.request('GET', '/api/videos')
                    response = client.getresponse()
                    results.append((response.status, json.loads(response.read())))
                finally:
                    client.close()
            worker = threading.Thread(target=load_videos)
            try:
                with patch.object(api, '_file_sha256', side_effect=slow_hash):
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(database.in_transaction)
                    for path in ('/healthz', '/readyz', '/api/todos', '/api/scripts'):
                        client = HTTPConnection(*server.server_address, timeout=0.7)
                        try:
                            client.request('GET', path)
                            response = client.getresponse()
                            self.assertEqual(response.status, 200, response.read())
                        finally:
                            client.close()
                    with lock:
                        media.write_bytes(b'replacement identity is a different length')
                        database.execute("UPDATE jobs SET title='fresh snapshot after hash' WHERE job_id='J'")
                        database.commit()
                    release.set(); worker.join(5)
                self.assertEqual(results[0][0], 200)
                self.assertIsNone(results[0][1]['jobs'][0]['final_sha256'])
                self.assertEqual(results[0][1]['jobs'][0]['title'], 'fresh snapshot after hash')
                self.assertFalse(service._final_hash_cache)
                observation = service.production_health(supervisor_alive=False)['management_io_observations'][-1]
                self.assertEqual(observation['step'], 'cold_media_sha')
                self.assertRegex(observation['target']['asset_key'], '^[0-9a-f]{24}$')
                self.assertNotIn(directory, json.dumps(observation))
                current = service.videos()['jobs'][0]['final_sha256']
                self.assertEqual(current, original(media))
            finally:
                release.set(); worker.join(5)
                server.shutdown(); server.server_close(); thread.join(2); database.close()

    def test_nested_read_snapshot_cache_miss_never_hashes_or_unlocks_transaction(self):
        import factory.management_api as api
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = self.make_service(directory)
            media = Path(directory) / 'data/assets/J/final.mp4'
            media.parent.mkdir(parents=True)
            media.write_bytes(b'cold nested asset')
            database.execute("INSERT INTO jobs(job_id,topic_id,title,state,final_path) VALUES('J','T','nested snapshot','COMPOSED',?)", (str(media),))
            database.commit()
            with lock:
                database.execute('SAVEPOINT caller_read_snapshot')
                try:
                    with patch.object(api, '_file_sha256', side_effect=AssertionError('snapshot must not hash')), \
                         patch.object(service, 'external_io', side_effect=AssertionError('snapshot must not unlock')):
                        job = service.videos()['jobs'][0]
                        self.assertIsNone(job['final_sha256'])
                        self.assertTrue(database.in_transaction)
                finally:
                    database.execute('RELEASE SAVEPOINT caller_read_snapshot')
            database.close()

    def test_http_stale_media_and_production_versions_return_readable_409_without_writes(self):
        from test_content_review import ContentReviewTests
        import hashlib
        fixture = ContentReviewTests('test_generic_candidate_accept_cannot_bypass_content')
        fixture.setUp()
        server = None
        try:
            final = fixture.base / 'final.mp4'
            final.write_bytes(b'new final')
            fixture.database.execute("UPDATE jobs SET state='COMPOSED',final_path=?,production_spec_hash=? WHERE job_id='J'", (str(final), 'a' * 64))
            fixture.database.commit()
            before = fixture.database.serialize()
            handler = type('VersionGuardHandler', (ConsoleHandler,), {'service': fixture.service,
                'service_lock': threading.RLock(), 'runtime_mode': 'desktop', 'auth_manager': None})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cases = [('/api/videos/J/masters/1/candidate-review',
                {'decision': 'accept_candidate', 'expected_candidate_sha256': hashlib.sha256(b'old candidate').hexdigest()}, 'candidate_media_conflict'),
                ('/api/videos/J/review', {'decision': 'approved', 'checks': {}, 'expected_final_sha256': hashlib.sha256(b'old final').hexdigest()}, 'final_media_conflict'),
                ('/api/videos/J/produce', {'confirmed': True, 'expected_production_spec_hash': 'b' * 64}, 'production_spec_conflict')]
            for route, payload, code in cases:
                with self.subTest(route=route):
                    client = HTTPConnection(*server.server_address, timeout=2)
                    try:
                        client.request('POST', route, body=json.dumps(payload), headers={'Content-Type': 'application/json'})
                        response = client.getresponse()
                        self.assertEqual(response.status, 409)
                        self.assertEqual(json.loads(response.read())['code'], code)
                        self.assertEqual(fixture.database.serialize(), before)
                    finally:
                        client.close()
        finally:
            if server:
                server.shutdown()
                server.server_close()
                thread.join(2)
            fixture.tearDown()

    def make_authenticated_server(self):
        from factory.server_auth import ServerSessionManager, hash_password
        manager = ServerSessionManager(session_secret='s' * 32,
            password_hash=hash_password('test-password', iterations=10000), public_base_url='https://video.example.com')
        issued = manager.login('test-password')
        service = Mock()
        handler = type('IsolatedAuthHandler', (ConsoleHandler,), {'service': service,
            'service_lock': threading.RLock(), 'runtime_mode': 'server', 'auth_manager': manager})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, service, issued

    def test_missing_csrf_rejections_remain_readable_with_unread_post_bodies(self):
        server, thread, service, issued = self.make_authenticated_server()
        try:
            for _ in range(12):
                client = HTTPConnection(*server.server_address, timeout=1)
                try:
                    client.request('POST', '/api/videos/J/produce', body='{"confirmed":true}',
                        headers={'Host': 'video.example.com', 'Origin': 'https://video.example.com',
                                 'Cookie': f'avf_session={issued.token}', 'Content-Type': 'application/json'})
                    response = client.getresponse()
                    self.assertEqual(response.status, 403)
                    self.assertIn('error', json.loads(response.read()))
                finally:
                    client.close()
            service.start_video_job.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_large_unauthorized_body_is_not_drained_or_allowed_to_block_health(self):
        server, thread, service, issued = self.make_authenticated_server()
        large = HTTPConnection(*server.server_address, timeout=0.7)
        try:
            large.putrequest('POST', '/api/videos/J/produce', skip_host=True)
            for name, value in {'Host': 'video.example.com', 'Origin': 'https://video.example.com',
                'Cookie': f'avf_session={issued.token}', 'Content-Type': 'application/json',
                'Content-Length': str(32 * 1024 * 1024)}.items():
                large.putheader(name, value)
            large.endheaders()
            response = large.getresponse()
            self.assertEqual(response.status, 403)
            self.assertIn('error', json.loads(response.read()))
            client = HTTPConnection(*server.server_address, timeout=0.7)
            try:
                client.request('GET', '/healthz', headers={'Host': 'video.example.com'})
                self.assertEqual(client.getresponse().status, 200)
            finally:
                client.close()
            service.start_video_job.assert_not_called()
        finally:
            large.close()
            server.shutdown()
            server.server_close()
            thread.join(2)
    def make_service(self, directory):
        from factory import db
        from factory.management_api import ManagementService
        database = sqlite3.connect(':memory:', check_same_thread=False)
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        service = ManagementService(database, Path(directory), worker_mode='external',
                                    env_path=Path(directory) / '.env')
        lock = threading.RLock()
        service.bind_service_lock(lock)
        return service, database, lock

    def assert_read_responsive(self, service, lock):
        handler = type('IsolatedReadHandler', (ConsoleHandler,), {
            'service': service, 'service_lock': lock, 'runtime_mode': 'desktop', 'auth_manager': None})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for route in ('/healthz', '/readyz', '/api/videos', '/api/todos'):
                client = HTTPConnection(*server.server_address, timeout=0.7)
                try:
                    client.request('GET', route)
                    response = client.getresponse()
                    self.assertEqual(response.status, 200, response.read())
                finally:
                    client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_slow_provider_inspection_releases_lock_and_discards_stale_lease(self):
        from factory.clients import seedance
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = self.make_service(directory)
            database.execute("INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J','T','test','PROVIDER_ACTION_REQUIRED')")
            database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,usage_json) VALUES('remote-1','J',1,'seedance','master',?)",
                             (json.dumps({'status': 'pending'}),))
            database.commit()
            entered, release = threading.Event(), threading.Event()
            async def inspect(task_id):
                entered.set()
                release.wait(5)
                return {'status': 'failed', 'cost_cents': 900}
            result = []
            def reconcile():
                with lock:
                    result.append(service.reconcile_provider_task('J', 'remote-1'))
            with patch.object(seedance, 'inspect_task', side_effect=inspect):
                worker = threading.Thread(target=reconcile)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assert_read_responsive(service, lock)
                    with lock:
                        database.execute("UPDATE jobs SET worker_lease_id='replacement' WHERE job_id='J'")
                        database.commit()
                finally:
                    release.set()
                    worker.join(5)
            self.assertFalse(result[0]['applied'])
            self.assertEqual(database.execute("SELECT cost_cents FROM jobs WHERE job_id='J'").fetchone()[0], 0)
            self.assertEqual(json.loads(database.execute("SELECT usage_json FROM provider_calls WHERE task_id='remote-1'").fetchone()[0])['status'], 'pending')
            observation = service.production_health(supervisor_alive=False)['management_io_observations'][-1]
            self.assertEqual(observation['step'], 'provider_inspection')
            self.assertEqual(observation['target'], {'job_id': 'J', 'task_id': 'remote-1'})
            database.close()

    def test_source_collection_releases_lock_and_does_not_apply_changed_feed(self):
        from factory import source_connectors
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = self.make_service(directory)
            database.execute("INSERT INTO source_feeds(feed_id,name,feed_type,endpoint) VALUES('F','test','rss','https://example.com/rss')")
            database.commit()
            entered, release = threading.Event(), threading.Event()
            def collect(cursor):
                entered.set()
                release.wait(5)
                return [], 'new-cursor'
            result = []
            def run():
                with lock:
                    result.append(service.collect_source_feed('F'))
            with patch.object(source_connectors, 'connector_for', return_value=Mock(collect=collect)):
                worker = threading.Thread(target=run)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assert_read_responsive(service, lock)
                    with lock:
                        database.execute("UPDATE source_feeds SET enabled=0 WHERE feed_id='F'")
                        database.commit()
                finally:
                    release.set()
                    worker.join(5)
            self.assertFalse(result[0]['applied'])
            feed = database.execute("SELECT enabled,cursor,last_status FROM source_feeds WHERE feed_id='F'").fetchone()
            self.assertEqual(tuple(feed), (0, None, 'never'))
            observation = service.production_health(supervisor_alive=False)['management_io_observations'][-1]
            self.assertEqual(observation['step'], 'source_collection')
            self.assertEqual(observation['target'], {'feed_id': 'F'})
            database.close()

    def test_external_adapter_rejected_inside_open_shared_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = self.make_service(directory)
            operation = Mock()
            database.execute('BEGIN')
            with lock, self.assertRaisesRegex(RuntimeError, 'SQLite transaction'):
                service.external_io(operation)
            operation.assert_not_called()
            database.rollback()
            database.close()

    def test_concurrent_feed_results_apply_once(self):
        from factory import source_connectors
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = self.make_service(directory)
            database.execute("INSERT INTO source_feeds(feed_id,name,feed_type,endpoint) VALUES('F','test','rss','https://example.com/rss')")
            database.commit()
            barrier = threading.Barrier(2)
            item = source_connectors.ConnectorItem(external_id='one', title='one',
                source_url='https://example.com/one', excerpt='body', published_at=None, raw={})
            def collect(cursor):
                barrier.wait(3)
                return [item], 'new-cursor'
            results = []
            def run():
                with lock:
                    results.append(service.collect_source_feed('F'))
            with patch.object(source_connectors, 'connector_for', return_value=Mock(collect=collect)):
                workers = [threading.Thread(target=run) for _ in range(2)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(5)
            self.assertEqual(len(results), 2)
            self.assertEqual(sum(r['created'] for r in results), 1)
            self.assertEqual(sum(r.get('stale', False) for r in results), 1)
            self.assertEqual(database.execute('SELECT COUNT(*) FROM source_candidates').fetchone()[0], 1)
            database.close()

    def test_stale_script_http_edit_returns_conflict_and_latest_content(self):
        from test_management_console import ManagementServiceTests
        fixture = ManagementServiceTests('test_candidate_script_can_be_edited_and_locks_after_approval')
        fixture.setUp()
        server = None
        database = sqlite3.connect(':memory:', check_same_thread=False)
        database.row_factory = sqlite3.Row
        try:
            fixture._insert_editable_content(approved=True)
            fixture.database.execute("UPDATE script_drafts SET revision=2 WHERE draft_id='SCRIPT-EDIT'")
            fixture.database.commit()
            fixture.database.backup(database)
            from factory.management_api import ManagementService
            service = ManagementService(database, fixture.root, worker_mode='external', env_path=fixture.root / '.env')
            handler = type('IsolatedRevisionHandler', (ConsoleHandler,), {
                'service': service, 'service_lock': threading.RLock(), 'runtime_mode': 'desktop', 'auth_manager': None})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            before = database.serialize()
            client = HTTPConnection(*server.server_address, timeout=2)
            try:
                client.request('POST', '/api/scripts/SCRIPT-EDIT/edit',
                    body=json.dumps({'script': fixture._editable_script_payload(title='stale'), 'expected_revision': 1}),
                    headers={'Content-Type': 'application/json'})
                response = client.getresponse()
                self.assertEqual(response.status, 409)
                payload = json.loads(response.read())
                self.assertEqual(payload['code'], 'script_revision_conflict')
                self.assertEqual(payload['current_revision'], 2)
                self.assertEqual(payload['expected_revision'], 1)
                self.assertNotEqual(payload['current_script']['title'], 'stale')
                self.assertEqual(database.serialize(), before)
            finally:
                client.close()
        finally:
            if server:
                server.shutdown()
                server.server_close()
                thread.join(2)
            database.close()
            fixture.tearDown()
            fixture.doCleanups()

    def test_slow_anchor_upload_releases_lock_and_preserves_stale_owner(self):
        from test_anchor_network_recovery import AnchorNetworkRecoveryTests
        from factory.management_api import ManagementService
        fixture = AnchorNetworkRecoveryTests('test_scene_download_reconciles_to_anchor_review_without_new_purchase')
        fixture.setUp()
        database = sqlite3.connect(':memory:', check_same_thread=False)
        database.row_factory = sqlite3.Row
        fixture.c.backup(database)
        try:
            service = ManagementService(database, fixture.root, worker_mode='external', data_dir=fixture.runtime_data,
                                        env_path=fixture.root / '.env')
            lock = threading.RLock()
            service.bind_service_lock(lock)
            entered, release = threading.Event(), threading.Event()
            def upload(*args, **kwargs):
                entered.set()
                release.wait(5)
                return 'https://example.invalid/anchor'
            errors = []
            def reconcile():
                try:
                    with lock:
                        service.reconcile_provider_task('J-ANCHOR', 'remote-anchor')
                except Exception as exc:
                    errors.append(exc)
            with patch('factory.management_api.seedance.inspect_task', return_value=fixture.completed()), \
                 patch('factory.clients.ffmpeg.extract_frames', side_effect=fixture.frames), \
                 patch('factory.clients.tos.upload', side_effect=upload):
                worker = threading.Thread(target=reconcile)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assert_read_responsive(service, lock)
                    with lock:
                        database.execute("UPDATE jobs SET worker_lease_id='replacement' WHERE job_id='J-ANCHOR'")
                        database.commit()
                finally:
                    release.set()
                    worker.join(5)
            self.assertEqual(len(errors), 1)
            self.assertIn('concurrently', str(errors[0]))
            self.assertEqual(database.execute('SELECT state FROM scene_visual_anchors').fetchone()[0], 'FAILED')
            self.assertEqual(database.execute('SELECT cost_cents FROM jobs').fetchone()[0], 0)
            observations = service.production_health(supervisor_alive=False)['management_io_observations']
            self.assertTrue({'provider_inspection', 'anchor_ffmpeg_extract', 'anchor_tos_upload'} <= {item['step'] for item in observations})
            for observation in observations:
                self.assertEqual(observation['target']['job_id'], 'J-ANCHOR')
                self.assertEqual(observation['target']['task_id'], 'remote-anchor')
        finally:
            database.close()
            fixture.doCleanups()

    def test_stalled_32mib_media_does_not_hold_business_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / 'large.mp4'
            with media.open('wb') as stream:
                stream.truncate(32 * 1024 * 1024)
            service = Mock()
            service.video_file.return_value = media
            service.overview.return_value = {'ok': True}
            request_done = threading.Event()
            def finish_observed_get(handler):
                try:
                    ConsoleHandler.do_GET(handler)
                finally:
                    if handler.path == '/api/videos/test/file':
                        request_done.set()
            handler = type('IsolatedHandler', (ConsoleHandler,), {
                'service': service, 'service_lock': threading.RLock(),
                'runtime_mode': 'desktop', 'auth_manager': None,
                'do_GET': finish_observed_get,
            })
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            slow = socket.socket()
            slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            slow.settimeout(2)
            slow.connect(server.server_address)
            try:
                slow.sendall(b'GET /api/videos/test/file HTTP/1.1\r\nHost: localhost\r\n\r\n')
                headers = b''
                while b'\r\n\r\n' not in headers:
                    headers += slow.recv(1)
                self.assertIn(b'33554432', headers)
                hold_started = time.perf_counter()
                for offset in (0, 5, 10, 15, 20, 25, 30):
                    deadline = hold_started + offset
                    while time.perf_counter() < deadline:
                        threading.Event().wait(min(5, deadline - time.perf_counter()))
                    for route in ('/healthz', '/readyz', '/api/overview'):
                        client = HTTPConnection(*server.server_address, timeout=0.7)
                        try:
                            request_started = time.perf_counter()
                            client.request('GET', route)
                            response = client.getresponse()
                            self.assertEqual(response.status, 200)
                            response.read()
                            self.assertLess(time.perf_counter() - request_started, 0.7)
                        finally:
                            client.close()
                self.assertGreaterEqual(time.perf_counter() - hold_started, 30)
            finally:
                slow.close()
                self.assertTrue(request_done.wait(2), 'stalled media stream did not release its file/handler after client close')
                server.shutdown()
                server.server_close()
                thread.join(2)


if __name__ == '__main__':
    unittest.main()
