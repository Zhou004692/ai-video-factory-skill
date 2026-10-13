"""Exact authenticated HTTP command receipts in a private temporary runtime."""
import json
import hashlib
import tempfile
import threading
import unittest
import uuid
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

import test_management_http_concurrency as http_fixture
ConsoleHandler = http_fixture.ConsoleHandler


class ManagementCommandTests(unittest.TestCase):
    def test_live_claim_cannot_be_orphaned_by_new_service_but_owner_end_can_be_resolved(self):
        from factory.management_api import ManagementService
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = http_fixture.HttpConcurrencyTests().make_service(directory)
            command_id = str(uuid.uuid4())
            path, digest = '/api/videos/J/produce', hashlib.sha256(b'{}').hexdigest()
            payload = {'path': path, 'request_sha256': digest, 'decision': 'not_executed', 'confirmed': True,
                       'reason': 'Owner request ended before execution', 'evidence': 'No production or remote intent exists'}
            try:
                service.claim_management_command(command_id, path, digest)
                restarted_view = ManagementService(database, Path(directory), worker_mode='external', env_path=Path(directory) / '.env')
                before = database.serialize()
                proof = restarted_view.management_command_result(command_id)
                self.assertEqual(proof['outcome'], 'pending')
                self.assertFalse(proof['orphaned'])
                with self.assertRaises(ValueError):
                    restarted_view.resolve_management_command(command_id, payload)
                self.assertEqual(database.serialize(), before)
                service.release_management_command(command_id)
                proof = restarted_view.management_command_result(command_id)
                self.assertEqual(proof['outcome'], 'unknown')
                self.assertTrue(proof['orphaned'])
                self.assertEqual(database.serialize(), before)
                resolved = restarted_view.resolve_management_command(command_id, payload)
                self.assertTrue(resolved['submission_known'])
                self.assertEqual(resolved['original_outcome'], 'pending')
                self.assertEqual(service.claim_management_command(command_id, path, digest)['disposition'], 'resolved')
            finally:
                database.close()

    def test_real_process_exit_leaves_pure_orphan_proof_and_can_be_human_resolved(self):
        import os
        import sqlite3
        import subprocess
        import sys
        from pathlib import Path
        from factory.management_api import ManagementService
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command_id = str(uuid.uuid4())
            path, digest = '/api/videos/J/produce', hashlib.sha256(b'{}').hexdigest()
            program = '''
import os,sqlite3,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from factory import db
from factory.management_api import ManagementService
database=sqlite3.connect(sys.argv[2]);database.row_factory=sqlite3.Row;database.executescript(db.DDL)
service=ManagementService(database,Path(sys.argv[3]),worker_mode='external',env_path=Path(sys.argv[3])/'.env')
service.claim_management_command(sys.argv[4],sys.argv[5],sys.argv[6])
os._exit(0)
'''
            env = {key: value for key, value in os.environ.items() if key.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','COMSPEC'}}
            env.update(FACTORY_RUNTIME_HOME=str(root / 'runtime'), FACTORY_ENV_FILE=str(root / '.env'),
                       FACTORY_DATA_DIR=str(root / 'data'), FACTORY_DB_PATH=str(root / 'receipt.db'))
            child = subprocess.run([sys.executable, '-X', 'utf8', '-c', program,
                str(Path(__file__).resolve().parents[1] / 'scripts'), str(root / 'receipt.db'), str(root), command_id, path, digest],
                env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(child.returncode, 0, child.stderr)
            database = sqlite3.connect(root / 'receipt.db'); database.row_factory = sqlite3.Row
            service = ManagementService(database, root, worker_mode='external', env_path=root / '.env')
            try:
                before = database.serialize()
                proof = service.management_command_result(command_id)
                self.assertEqual(proof['outcome'], 'unknown')
                self.assertTrue(proof['orphaned'])
                self.assertEqual(database.serialize(), before)
                service.resolve_management_command(command_id, {'path': path, 'request_sha256': digest,
                    'decision': 'not_executed', 'confirmed': True, 'reason': 'Verified previous owner process exited',
                    'evidence': 'No worker queue or provider ledger was created'})
                self.assertEqual(service.claim_management_command(command_id, path, digest)['disposition'], 'resolved')
            finally:
                database.close()

    def test_nonexecution_resolution_blocks_queue_lease_native_and_platform_intents(self):
        scenarios = [
            ("UPDATE jobs SET state='PRODUCTION_QUEUED' WHERE job_id='J'", 'active_production_queue'),
            ("UPDATE jobs SET worker_lease_id='current',production_heartbeat_at=datetime('now') WHERE job_id='J'", 'active_worker_lease'),
            ("INSERT INTO events(job_id,step,message) VALUES('J','native_visual_repair_authorized','{\"attempt\":{\"attempt_id\":\"A\"}}')", 'unclosed_native_authorization'),
            ("INSERT INTO platform_submissions(submission_id,job_id,platform,state) VALUES('SUB','J','douyin','uploading')", 'unresolved_platform_submission'),
            ("INSERT INTO platform_publications(publication_id,job_id,platform,item_id,metadata_json) VALUES('PUB','J','douyin','item','{\"review_status\":\"pending\"}')", 'pending_platform_publication'),
        ]
        for statement, code in scenarios:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                service, database, lock = http_fixture.HttpConcurrencyTests().make_service(directory)
                try:
                    database.execute("INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J','T','guard','STORYBOARDED')")
                    database.execute(statement); database.commit()
                    command_id = str(uuid.uuid4())
                    path, digest = '/api/videos/J/produce', hashlib.sha256(b'{}').hexdigest()
                    service.claim_management_command(command_id, path, digest)
                    service.finish_management_command(command_id, {}, 500)
                    before = database.serialize()
                    with self.assertRaises(ValueError) as conflict:
                        service.resolve_management_command(command_id, {'path': path, 'request_sha256': digest,
                            'decision': 'not_executed', 'confirmed': True, 'reason': 'Operator assertion', 'evidence': 'Checked records'})
                    self.assertIn(code, {item['code'] for item in conflict.exception.payload['blockers']})
                    self.assertEqual(database.serialize(), before)
                finally:
                    database.close()

    def test_terminal_http_unknown_can_be_resolved_without_rewriting_or_reexecuting_original(self):
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = http_fixture.HttpConcurrencyTests().make_service(directory)
            database.execute("INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J','T','safe unknown','STORYBOARDED')")
            database.commit()
            service.start_video_job = Mock(side_effect=RuntimeError('no effects before failure'))
            handler = type('HumanResolutionHandler', (ConsoleHandler,), {'service': service,
                'service_lock': lock, 'runtime_mode': 'desktop', 'auth_manager': None})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            command_id = str(uuid.uuid4())
            path, body = '/api/videos/J/produce', '{"confirmed":true}'
            digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
            def request(method, route, raw=None, headers=None):
                client = HTTPConnection(*server.server_address, timeout=2)
                try:
                    client.request(method, route, body=raw, headers={'Content-Type': 'application/json', **(headers or {})})
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    client.close()
            try:
                status, failed = request('POST', path, body, {'X-AVF-Command-Id': command_id, 'X-AVF-Command-Hash': digest})
                self.assertEqual(status, 500)
                original = [tuple(row) for row in database.execute("SELECT * FROM events WHERE step='management_command_result'")]
                production = [tuple(row) for row in database.execute('SELECT * FROM jobs')]
                resolve = {'path': path, 'request_sha256': digest, 'decision': 'not_executed', 'confirmed': True,
                    'reason': 'Operator checked no command execution', 'evidence': 'No queue or provider entries; https://private-user:private-password@media.example.com/final.mp4?X-Amz-Credential=private-credential&X-Amz-Signature=private-signature#private-fragment ; ARK_API_KEY=do-not-store-this-secret'}
                status, proof = request('POST', f'/api/commands/{command_id}/resolve', json.dumps(resolve))
                self.assertEqual(status, 200)
                self.assertTrue(proof['submission_known'])
                self.assertEqual(proof['outcome'], 'rejected')
                self.assertEqual(proof['resolution']['source'], 'human')
                self.assertEqual(proof['original_http_status'], 500)
                self.assertEqual(proof['original_outcome'], 'unknown')
                self.assertNotIn('do-not-store-this-secret', json.dumps(proof))
                for value in ('private-user', 'private-password', 'private-credential', 'private-signature', 'private-fragment', 'X-Amz'):
                    self.assertNotIn(value, json.dumps(proof))
                self.assertIn('https://media.example.com/final.mp4', proof['resolution']['evidence'])
                before = database.serialize()
                status, repeat = request('POST', f'/api/commands/{command_id}/resolve', json.dumps(resolve))
                self.assertEqual(status, 200)
                self.assertEqual(repeat, proof)
                self.assertEqual(database.serialize(), before)
                self.assertEqual(service.finish_management_command(command_id, {}, 200)['outcome'], 'unknown')
                self.assertEqual(database.serialize(), before)
                status, conflict = request('POST', f'/api/commands/{command_id}/resolve', json.dumps({**resolve, 'decision': 'executed'}))
                self.assertEqual(status, 409)
                self.assertEqual(conflict['code'], 'command_resolution_conflict')
                status, read_proof = request('GET', f'/api/commands/{command_id}')
                self.assertEqual(read_proof, proof)
                status, replay = request('POST', path, body, {'X-AVF-Command-Id': command_id, 'X-AVF-Command-Hash': digest})
                self.assertEqual(status, 409)
                self.assertEqual(replay['code'], 'command_already_resolved')
                service.start_video_job.assert_called_once()
                self.assertEqual([tuple(row) for row in database.execute("SELECT * FROM events WHERE step='management_command_result'")], original)
                self.assertEqual([tuple(row) for row in database.execute('SELECT * FROM jobs')], production)
                self.assertEqual(database.execute("SELECT COUNT(*) FROM events WHERE step='management_command_resolution'").fetchone()[0], 1)
            finally:
                server.shutdown(); server.server_close(); thread.join(2); database.close()

    def test_only_terminal_unknown_is_resolvable_and_not_executed_cannot_override_live_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = http_fixture.HttpConcurrencyTests().make_service(directory)
            database.execute("INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J','T','guard','STORYBOARDED')")
            database.commit()
            path, digest = '/api/videos/J/produce', hashlib.sha256(b'{}').hexdigest()
            payload = {'path': path, 'request_sha256': digest, 'decision': 'not_executed', 'confirmed': True,
                       'reason': 'Verified request execution', 'evidence': 'Checked local and remote records'}
            try:
                for status in (None, 200, 409):
                    command_id = str(uuid.uuid4())
                    service.claim_management_command(command_id, path, digest)
                    if status:
                        service.finish_management_command(command_id, {}, status)
                    before = database.serialize()
                    with self.assertRaises(ValueError):
                        service.resolve_management_command(command_id, payload)
                    self.assertEqual(database.serialize(), before)
                command_id = str(uuid.uuid4())
                service.claim_management_command(command_id, path, digest)
                service.finish_management_command(command_id, {}, 500)
                database.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,usage_json) VALUES('local-submit:pending','J',1,'seedance','{\"status\":\"unknown\"}')")
                database.commit()
                before = database.serialize()
                with self.assertRaises(ValueError) as blocked:
                    service.resolve_management_command(command_id, payload)
                self.assertEqual(blocked.exception.payload['code'], 'command_resolution_blocked')
                self.assertEqual(database.serialize(), before)
                proof = service.resolve_management_command(command_id, {**payload, 'decision': 'executed'})
                self.assertEqual(proof['outcome'], 'accepted')
                self.assertTrue(proof['submission_known'])
                self.assertEqual(database.execute('SELECT usage_json FROM provider_calls').fetchone()[0], '{"status":"unknown"}')
                self.assertEqual(database.execute('SELECT state FROM jobs').fetchone()[0], 'STORYBOARDED')
            finally:
                database.close()

    def test_receipt_queries_and_cancellations_keep_authentication_and_csrf_boundary(self):
        server, thread, service, issued = http_fixture.HttpConcurrencyTests().make_authenticated_server()
        try:
            command_id = str(uuid.uuid4())
            for method, path, headers, body, expected in (
                ('GET', '/api/commands/' + command_id, {}, None, 401),
                ('POST', f'/api/commands/{command_id}/cancel-unclaimed',
                 {'Cookie': f'avf_session={issued.token}', 'Origin': 'https://video.example.com'}, '{}', 403),
                ('POST', f'/api/commands/{command_id}/resolve', {}, '{}', 403),
                ('POST', f'/api/commands/{command_id}/resolve',
                 {'Cookie': f'avf_session={issued.token}', 'Origin': 'https://video.example.com'}, '{}', 403),
            ):
                client = HTTPConnection(*server.server_address, timeout=2)
                try:
                    client.request(method, path, body=body, headers={'Host': 'video.example.com', 'Content-Type': 'application/json', **headers})
                    response = client.getresponse()
                    self.assertEqual(response.status, expected)
                    self.assertIn('error', json.loads(response.read()))
                finally:
                    client.close()
            service.management_command_result.assert_not_called()
            service.cancel_unclaimed_management_command.assert_not_called()
            service.resolve_management_command.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_lost_success_response_is_queryable_and_same_id_never_reexecutes(self):
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = http_fixture.HttpConcurrencyTests().make_service(directory)
            command_id = str(uuid.uuid4())
            service.start_video_job = Mock(return_value={'status': 'queued', 'job_id': 'J', 'state': 'PRODUCTION_QUEUED'})
            class LostResponseHandler(ConsoleHandler):
                def end_headers(self):
                    if self.command == 'POST' and self.headers.get('X-Test-Lose-Response'):
                        raise BrokenPipeError('simulated disconnected client after durable acceptance')
                    return super().end_headers()
            handler = type('ReceiptHandler', (LostResponseHandler,), {'service': service,
                'service_lock': lock, 'runtime_mode': 'desktop', 'auth_manager': None})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, path, payload=None, **headers):
                client = HTTPConnection(*server.server_address, timeout=2)
                try:
                    body = json.dumps(payload) if payload is not None else None
                    if 'X-AVF-Command-Id' in headers:
                        headers['X-AVF-Command-Hash'] = hashlib.sha256((body or '').encode('utf-8')).hexdigest()
                    client.request(method, path, body=body,
                        headers={'Content-Type': 'application/json', **headers})
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    client.close()
            try:
                with self.assertRaises((ConnectionError, OSError)):
                    request('POST', '/api/videos/J/produce', {'confirmed': True},
                        **{'X-AVF-Command-Id': command_id, 'X-Test-Lose-Response': 'true'})
                status, receipt = request('GET', '/api/commands/' + command_id)
                self.assertEqual(status, 200)
                self.assertEqual(receipt['command_id'], command_id)
                self.assertEqual(receipt['outcome'], 'accepted')
                self.assertTrue(receipt['submission_known'])
                self.assertEqual(receipt['path'], '/api/videos/J/produce')
                self.assertEqual(len(receipt['request_sha256']), 64)
                self.assertEqual(receipt['request_sha256'], hashlib.sha256(json.dumps({'confirmed': True}).encode('utf-8')).hexdigest())
                status, replay = request('POST', '/api/videos/J/produce', {'confirmed': True}, **{'X-AVF-Command-Id': command_id})
                self.assertEqual(status, 200)
                self.assertTrue(replay['replayed'])
                service.start_video_job.assert_called_once()
                status, conflict = request('POST', '/api/videos/J/produce', {'confirmed': True, 'approved_budget_cents': 20000}, **{'X-AVF-Command-Id': command_id})
                self.assertEqual(status, 409)
                self.assertEqual(conflict['code'], 'command_identity_conflict')
                service.start_video_job.assert_called_once()
                messages = [json.loads(row[0]) for row in database.execute("SELECT message FROM events WHERE step LIKE 'management_command_%'")]
                self.assertEqual(len(messages), 2)
                self.assertTrue(all('confirmed' not in message and 'body' not in message for message in messages))
            finally:
                server.shutdown(); server.server_close(); thread.join(2); database.close()

    def test_body_digest_uses_exact_utf8_bytes_and_rejects_wrong_header_before_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = http_fixture.HttpConcurrencyTests().make_service(directory)
            service.start_video_job = Mock(return_value={'status': 'queued'})
            handler = type('CommandBytesHandler', (ConsoleHandler,), {'service': service,
                'service_lock': lock, 'runtime_mode': 'desktop', 'auth_manager': None})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                for body in ('{"confirmed":true,"note":"中文","decimal":1.0,"small":1e-7}', ''):
                    command_id = str(uuid.uuid4())
                    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
                    client = HTTPConnection(*server.server_address, timeout=2)
                    client.request('POST', '/api/videos/J/produce', body=body.encode('utf-8'), headers={'Content-Type': 'application/json',
                        'X-AVF-Command-Id': command_id, 'X-AVF-Command-Hash': digest})
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())['command_receipt']['request_sha256'], digest)
                    client.close()
                before = database.serialize()
                client = HTTPConnection(*server.server_address, timeout=2)
                client.request('POST', '/api/videos/J/produce', body='{"confirmed":true}', headers={'Content-Type': 'application/json',
                    'X-AVF-Command-Id': str(uuid.uuid4()), 'X-AVF-Command-Hash': '0' * 64})
                response = client.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read())['code'], 'command_hash_mismatch')
                client.close()
                self.assertEqual(database.serialize(), before)
                self.assertEqual(service.start_video_job.call_count, 2)
            finally:
                server.shutdown(); server.server_close(); thread.join(2); database.close()

    def test_pending_unknown_and_rejected_commands_have_distinct_exact_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = http_fixture.HttpConcurrencyTests().make_service(directory)
            try:
                command_id = str(uuid.uuid4())
                payload = hashlib.sha256(json.dumps({'confirmed': True}).encode('utf-8')).hexdigest()
                claim = service.claim_management_command(command_id, '/api/videos/J/produce', payload)
                self.assertEqual(claim['disposition'], 'execute')
                pending = service.claim_management_command(command_id, '/api/videos/J/produce', payload)
                self.assertEqual(pending['code'], 'submission_pending')
                with self.assertRaises(ValueError):
                    service.claim_management_command(command_id, '/api/videos/K/produce', payload)
                service.finish_management_command(command_id, {'error': 'provider uncertainty'}, 500)
                unknown = service.management_command_result(command_id)
                self.assertEqual(unknown['outcome'], 'unknown')
                self.assertFalse(unknown['submission_known'])
                self.assertEqual(service.claim_management_command(command_id, '/api/videos/J/produce', payload)['disposition'], 'replay')
                rejected_id = str(uuid.uuid4())
                service.claim_management_command(rejected_id, '/api/videos/J/produce', payload)
                service.finish_management_command(rejected_id, {'code': 'production_spec_conflict'}, 409)
                rejection = service.management_command_result(rejected_id)
                self.assertTrue(rejection['submission_known'])
                self.assertEqual(rejection['outcome'], 'rejected')
                before = database.serialize()
                service.management_command_result(command_id)
                self.assertEqual(database.serialize(), before)
            finally:
                database.close()

    def test_cancelled_unclaimed_http_command_cannot_execute_when_it_arrives_late(self):
        from factory.services.management_commands import request_digest
        with tempfile.TemporaryDirectory() as directory:
            service, database, lock = http_fixture.HttpConcurrencyTests().make_service(directory)
            service.start_video_job = Mock(return_value={'status': 'queued'})
            command_id = str(uuid.uuid4())
            path, payload = '/api/videos/J/produce', {'confirmed': True}
            handler = type('CancelledCommandHandler', (ConsoleHandler,), {'service': service,
                'service_lock': lock, 'runtime_mode': 'desktop', 'auth_manager': None})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                client = HTTPConnection(*server.server_address, timeout=2)
                client.request('POST', f'/api/commands/{command_id}/cancel-unclaimed',
                    body=json.dumps({'path': path, 'request_sha256': request_digest(json.dumps(payload).encode('utf-8'))}), headers={'Content-Type': 'application/json'})
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())['submission_known'])
                client.close()
                before = database.serialize()
                client = HTTPConnection(*server.server_address, timeout=2)
                client.request('POST', path, body=json.dumps(payload), headers={'Content-Type': 'application/json', 'X-AVF-Command-Id': command_id})
                response = client.getresponse()
                self.assertEqual(response.status, 409)
                self.assertEqual(json.loads(response.read())['code'], 'command_cancelled')
                client.close()
                service.start_video_job.assert_not_called()
                self.assertEqual(database.serialize(), before)
                claimed_id = str(uuid.uuid4())
                service.claim_management_command(claimed_id, path, request_digest(json.dumps(payload).encode('utf-8')))
                before = database.serialize()
                refusal = service.cancel_unclaimed_management_command(claimed_id, path, request_digest(json.dumps(payload).encode('utf-8')))
                self.assertEqual(refusal['code'], 'submission_pending')
                self.assertFalse(refusal['submission_known'])
                self.assertEqual(database.serialize(), before)
            finally:
                server.shutdown(); server.server_close(); thread.join(2); database.close()
