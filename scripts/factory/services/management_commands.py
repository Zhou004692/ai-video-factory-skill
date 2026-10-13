"""HTTP acknowledgment receipts, backed by existing immutable event records.

These records describe a single request's execution, not production state.
No request body, credentials, or remote response body is stored.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit
from factory import node_state_machine

_PROCESS_EPOCH = str(uuid.uuid4())
_ACTIVE_COMMANDS = set()
_ACTIVE_LOCK = threading.RLock()


def _synchronized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _ACTIVE_LOCK:
            return function(*args, **kwargs)
    return wrapped


def _database_key(database):
    row = next(row for row in database.execute('PRAGMA database_list') if row[1] == 'main')
    return os.path.normcase(str(Path(row[2]).resolve())) if row[2] else ('memory', id(database))


def _owner_alive(pid):
    """Read-only PID observation; permission/unknown failures remain live."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            exit_code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _pending_proof(database, claim):
    pid, epoch = claim.get('owner_pid'), claim.get('owner_epoch')
    valid_owner = isinstance(pid, int) and 0 < pid <= 0xffffffff and isinstance(epoch, str)
    orphaned = False
    if valid_owner:
        if pid == os.getpid():
            orphaned = epoch != _PROCESS_EPOCH or (_database_key(database), claim['command_id']) not in _ACTIVE_COMMANDS
        else:
            orphaned = not _owner_alive(pid)
    return {**claim, 'outcome': 'unknown' if orphaned else 'pending', 'http_status': None,
            'submission_known': False, 'orphaned': orphaned,
            'code': 'command_orphaned' if orphaned else 'submission_pending',
            'owner_evidence': 'observed' if valid_owner else 'missing'}


class CommandResolutionError(ValueError):
    def __init__(self, code, message, **fields):
        super().__init__(message)
        self.payload = {'code': code, 'error': message, 'submission_known': False, **fields}


class CommandIdentityConflict(ValueError):
    def __init__(self, command_id):
        super().__init__('Command identity does not match the original request')
        self.payload = {'code': 'command_identity_conflict', 'command_id': command_id,
                        'submission_known': False, 'error': str(self)}


def normalized_id(value):
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('X-AVF-Command-Id must be a UUID') from exc
    return str(parsed)


def request_digest(raw_body: bytes):
    return hashlib.sha256(raw_body).hexdigest()


def _event(database, command_id, kind):
    row = database.execute('SELECT message FROM events WHERE step=? AND level=? ORDER BY id LIMIT 1',
        (kind, 'command:' + command_id)).fetchone()
    return json.loads(row['message']) if row else None


@_synchronized
def read(database, command_id):
    command_id = normalized_id(command_id)
    resolution = _event(database, command_id, 'management_command_resolution')
    if resolution:
        return resolution
    result = _event(database, command_id, 'management_command_result')
    if result:
        return result
    claim = _event(database, command_id, 'management_command_claim')
    if not claim:
        return {'command_id': command_id, 'code': 'command_not_found', 'submission_known': False}
    return _pending_proof(database, claim)


@_synchronized
def claim(database, command_id, path, digest):
    command_id = normalized_id(command_id)
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('request_sha256 must be a lowercase SHA256 digest')
    # Claims must be durable before external work starts. Never commit someone
    # else's uncommitted business changes as part of an HTTP receipt.
    if database.in_transaction:
        raise RuntimeError('Command claim requires a closed SQLite transaction')
    with node_state_machine.atomic(database):
        prior = _event(database, command_id, 'management_command_claim')
        if prior:
            if prior['path'] != path or prior['request_sha256'] != digest:
                raise CommandIdentityConflict(command_id)
            resolution = _event(database, command_id, 'management_command_resolution')
            if resolution:
                return {'disposition': 'resolved', 'receipt': resolution}
            result = _event(database, command_id, 'management_command_result')
            if result:
                return {'disposition': 'replay', 'receipt': result}
            return {'disposition': 'pending', **read(database, command_id)}
        entry = {'command_id': command_id, 'path': path, 'request_sha256': digest,
                 'owner_pid': os.getpid(), 'owner_epoch': _PROCESS_EPOCH}
        parts = path.split('/')
        job_id = parts[3] if len(parts) > 3 and parts[2] == 'videos' else None
        database.execute('INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)',
            (job_id, 'management_command_claim', 'command:' + command_id, json.dumps(entry)))
    _ACTIVE_COMMANDS.add((_database_key(database), command_id))
    return {'disposition': 'execute', **entry}


@_synchronized
def finish(database, command_id, payload, status):
    command_id = normalized_id(command_id)
    if database.in_transaction:
        raise RuntimeError('Command result requires a closed SQLite transaction')
    with node_state_machine.atomic(database):
        prior = _event(database, command_id, 'management_command_result')
        if prior:
            _ACTIVE_COMMANDS.discard((_database_key(database), command_id))
            return prior
        entry = _event(database, command_id, 'management_command_claim')
        if not entry:
            raise RuntimeError('Cannot acknowledge an unclaimed command')
        outcome = 'accepted' if 200 <= status < 300 else ('rejected' if 400 <= status < 500 else 'unknown')
        # Store only a deliberately narrow acknowledgment. Error strings and
        # business objects may contain input/provider data and are excluded.
        summary = {key: payload[key] for key in ('job_id', 'state', 'status', 'code', 'cached')
                   if key in payload and isinstance(payload[key], (str, bool, int, type(None)))}
        result = {**entry, 'http_status': int(status), 'outcome': outcome,
                  'submission_known': outcome != 'unknown', 'result': summary}
        database.execute('INSERT INTO events(step,level,message) VALUES(?,?,?)',
            ('management_command_result', 'command:' + command_id, json.dumps(result)))
        _ACTIVE_COMMANDS.discard((_database_key(database), command_id))
        return result


@_synchronized
def cancel_unclaimed(database, command_id, path, digest):
    command_id = normalized_id(command_id)
    if not isinstance(path, str) or not path.startswith('/api/') or path.startswith(('/api/auth/', '/api/commands/')) or '?' in path:
        raise ValueError('A business API path is required')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('request_sha256 must be a lowercase SHA256 digest')
    if database.in_transaction:
        raise RuntimeError('Command cancellation requires a closed SQLite transaction')
    with node_state_machine.atomic(database):
        prior = _event(database, command_id, 'management_command_claim')
        if prior:
            if prior['path'] != path or prior['request_sha256'] != digest:
                raise CommandIdentityConflict(command_id)
            result = _event(database, command_id, 'management_command_result')
            if result and result['result'].get('code') == 'command_cancelled':
                return result
            return {'command_id': command_id, 'code': 'submission_pending', 'submission_known': False}
        entry = {'command_id': command_id, 'path': path, 'request_sha256': digest}
        result = {**entry, 'http_status': 409, 'outcome': 'rejected', 'submission_known': True,
                  'result': {'code': 'command_cancelled'}}
        for kind, record in [('management_command_claim', entry), ('management_command_result', result)]:
            database.execute('INSERT INTO events(step,level,message) VALUES(?,?,?)',
                (kind, 'command:' + command_id, json.dumps(record)))
        return result


@_synchronized
def release(database, command_id):
    _ACTIVE_COMMANDS.discard((_database_key(database), normalized_id(command_id)))


def _object(raw):
    try:
        value = json.loads(raw or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _redacted_text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError(name + ' is required and must be at most 2048 characters')
    value = re.sub('[\x00-\x08\x0b-\x1f\x7f]', ' ', value.strip())
    from factory.transport_diagnostics import _safe_host
    def strip_url(match):
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
            host = _safe_host(raw)
            if host == 'unknown':
                return '[REDACTED URL]'
            host = '[' + host + ']' if ':' in host else host
            return parsed.scheme.lower() + '://' + host + parsed.path
        except ValueError:
            return '[REDACTED URL]'
    # Strip every query/fragment and URL userinfo, not a denylist of signing
    # parameter names. The existing diagnostic host sanitizer is reused.
    value = re.sub(r'(?i)\bhttps?://[^\s<>\"\x27]+', strip_url, value)
    value = re.sub(r'(?i)\b(?:authorization|cookie|set-cookie|password|[a-z0-9_]*(?:api[_-]?key|token|secret)|volc[_-]?(?:ak|sk))[\"\x27]?\s*[:=]\s*[^\r\n;,]+', '[REDACTED]', value)
    value = re.sub(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|$)', '[REDACTED]', value, flags=re.S)
    value = re.sub(r'(?i)\bBearer\s+[^\s]+', 'Bearer [REDACTED]', value)
    return re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED]', value)


def _non_execution_blockers(database, path):
    # Bind only the job identity in the immutable originally claimed route.
    match = re.fullmatch(r'/api/videos/([^/?#]+)(?:/[^?#]+)?', path)
    if not match:
        return [{'code': 'target_evidence_unavailable'}]
    job_id = match.group(1)
    blockers = []
    def block(code):
        blockers.append({'code': code, 'job_id': job_id})
    job = database.execute('SELECT state,worker_lease_id,production_heartbeat_at FROM jobs WHERE job_id=?', (job_id,)).fetchone()
    if job:
        if job['state'] in {'PRODUCTION_QUEUED', 'PRODUCTION_RUNNING'}:
            block('active_production_queue')
        if job['worker_lease_id']:
            try:
                stamp = datetime.fromisoformat(str(job['production_heartbeat_at'] or '').replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                active_lease = (datetime.now(timezone.utc) - stamp).total_seconds() <= 60
            except ValueError:
                active_lease = True
            if active_lease:
                block('active_worker_lease')
    nodes = database.execute('SELECT state,provider_task_id,node_type,output_json FROM production_nodes WHERE job_id=?', (job_id,)).fetchall()
    if any(row['state'] == 'RUNNING' or (row['provider_task_id'] and row['state'] not in {'SUCCEEDED', 'FAILED', 'SKIPPED'}) for row in nodes):
        block('active_or_unverified_node')
    from factory import provider_tasks, native_visual_repair
    unresolved = set(provider_tasks.unresolved_task_ids(database, job_id))
    for row in database.execute('SELECT task_id,usage_json FROM provider_calls WHERE job_id=?', (job_id,)):
        status = str(_object(row['usage_json']).get('status') or '').casefold()
        if status in {'', 'accepted', 'running', 'queued', 'processing'}:
            unresolved.add(str(row['task_id']))
    if unresolved:
        block('unresolved_provider_task')
    closed = {_object(row['message']).get('attempt_id') for row in database.execute('SELECT message FROM events WHERE job_id=? AND step=?', (job_id, native_visual_repair.CLOSED))}
    authorizations = []
    for row in database.execute('SELECT message FROM events WHERE job_id=? AND step=?', (job_id, native_visual_repair.AUTHORIZED)):
        attempt = _object(row['message']).get('attempt')
        authorizations.append(attempt.get('attempt_id') if isinstance(attempt, dict) else None)
    if any(identity is None or identity not in closed for identity in authorizations):
        block('unclosed_native_authorization')
    for row in database.execute('SELECT state,upload_id,item_id,metadata_json FROM platform_submissions WHERE job_id=?', (job_id,)):
        metadata = _object(row['metadata_json'])
        if row['state'] in {'uploading', 'submitted', 'pending_review'} or (row['state'] == 'draft' and (row['upload_id'] or row['item_id'] or metadata.get('upload_binding') or metadata.get('submission_intent') or metadata.get('publish_intent') or metadata.get('submit_intent'))):
            block('unresolved_platform_submission')
            break
    from factory.management_api import _publication_is_pending
    if any(_publication_is_pending(row['metadata_json']) for row in database.execute('SELECT metadata_json FROM platform_publications WHERE job_id=? AND is_current=1', (job_id,))):
        block('pending_platform_publication')
    return blockers


@_synchronized
def resolve(database, command_id, payload):
    command_id = normalized_id(command_id)
    if payload.get('confirmed') is not True or payload.get('decision') not in {'executed', 'not_executed'}:
        raise ValueError('Explicit confirmation and executed/not_executed decision are required')
    reason = _redacted_text(payload.get('reason'), 'reason')
    evidence = _redacted_text(payload.get('evidence'), 'evidence')
    fingerprint = hashlib.sha256(json.dumps({key: payload.get(key) for key in ('decision', 'reason', 'evidence')},
        ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    if database.in_transaction:
        raise RuntimeError('Command resolution requires a closed SQLite transaction')
    with node_state_machine.atomic(database):
        original_claim = _event(database, command_id, 'management_command_claim')
        if not original_claim or original_claim['path'] != payload.get('path') or original_claim['request_sha256'] != payload.get('request_sha256'):
            raise CommandIdentityConflict(command_id)
        prior = _event(database, command_id, 'management_command_resolution')
        if prior:
            if prior['resolution']['request_fingerprint'] != fingerprint:
                raise CommandResolutionError('command_resolution_conflict', 'Command already has a different human resolution')
            return prior
        original = _event(database, command_id, 'management_command_result')
        observed = read(database, command_id)
        if not ((original and original['outcome'] == 'unknown') or (not original and observed.get('orphaned') is True)):
            raise CommandResolutionError('command_resolution_not_eligible', 'Only terminal unknown or proven orphaned commands can be resolved')
        if payload['decision'] == 'not_executed':
            blockers = _non_execution_blockers(database, original_claim['path'])
            if blockers:
                raise CommandResolutionError('command_resolution_blocked', 'Existing execution or remote intent requires reconciliation first', blockers=blockers)
        result = {**original_claim, 'http_status': 200 if payload['decision'] == 'executed' else 409,
            'outcome': 'accepted' if payload['decision'] == 'executed' else 'rejected', 'submission_known': True,
            'original_http_status': original['http_status'] if original else None,
            'original_outcome': original['outcome'] if original else 'pending',
            'orphaned': bool(observed.get('orphaned')),
            'resolution': {'source': 'human', 'decision': payload['decision'], 'reason': reason, 'evidence': evidence,
                           'request_fingerprint': fingerprint},
            'result': {'code': 'command_human_resolved'}}
        database.execute('INSERT INTO events(step,level,message) VALUES(?,?,?)',
            ('management_command_resolution', 'command:' + command_id, json.dumps(result)))
        return result
