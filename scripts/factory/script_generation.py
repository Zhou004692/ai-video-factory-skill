"""Durable, bounded generation of independently reviewable script variants.

Each public mutation owns a short transaction. Provider IO is deliberately outside
that transaction and every result is checked against the frozen input snapshot.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import uuid

from . import script_drafts
from .clients import doubao_llm

PROMPT_VERSION = 'single-complete-variant-v1'
TARGET_COUNT = 3
MAX_ATTEMPTS_PER_VARIANT = 2
MAX_OUTPUT_TOKENS = 12000
RECOMMENDATION_MAX_TOKENS = 1500


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _clean(db):
    if db.in_transaction:
        raise RuntimeError('剧本生成需要独立事务，请先提交当前业务操作')


@contextmanager
def _transaction(db):
    _clean(db)
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def ensure_schema(db):
    """Install run tables without implicitly committing another owner's work."""
    _clean(db)
    statements = [
        '''CREATE TABLE IF NOT EXISTS script_generation_runs (
        run_id TEXT PRIMARY KEY, brief_id TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
        snapshot_json TEXT NOT NULL, brief_revision INTEGER NOT NULL DEFAULT 1,
        angle_index INTEGER,
        status TEXT NOT NULL DEFAULT 'queued',
        active_variant INTEGER, error_code TEXT, error_message TEXT,
        recommended_index INTEGER, recommendation_reason TEXT,
        unranked_reason TEXT, recommendation_done INTEGER NOT NULL DEFAULT 0,
        legacy_cached INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS script_generation_slots (
        run_id TEXT NOT NULL, variant_index INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', draft_id TEXT, draft_hash TEXT,
        draft_revision INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(run_id,variant_index))''',
        '''CREATE TABLE IF NOT EXISTS script_generation_attempts (
        attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, variant_index INTEGER NOT NULL,
        attempt_number INTEGER NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
        error_code TEXT, error_message TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0, diagnostic_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
        UNIQUE(run_id,variant_index,attempt_number))''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS script_generation_one_inflight
        ON script_generation_attempts(run_id) WHERE status='in_flight' ''',
    ]
    with _transaction(db):
        for statement in statements:
            db.execute(statement)
        # Additive migration for a database opened by an earlier local build.
        for table, field in (('script_generation_runs', 'brief_revision'), ('script_generation_slots', 'draft_revision')):
            columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            if field not in columns:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {field} INTEGER NOT NULL DEFAULT 1')
        if 'angle_index' not in {row[1] for row in db.execute('PRAGMA table_info(script_generation_runs)')}:
            db.execute('ALTER TABLE script_generation_runs ADD COLUMN angle_index INTEGER')


def _snapshot(db, brief_id, preferences, model, max_output_tokens=MAX_OUTPUT_TOKENS):
    context, sources = script_drafts._load_context(db, brief_id)
    row = dict(db.execute('SELECT * FROM topic_briefs WHERE brief_id=?', (brief_id,)).fetchone())
    # Retain complete source metadata, not just the subset used by legacy prompts.
    for source in sources:
        raw = dict(db.execute('SELECT * FROM script_sources WHERE source_id=?', (source['source_id'],)).fetchone())
        source['metadata_json'] = raw.get('metadata_json') or '{}'
        source['content_sha256'] = hashlib.sha256(str(source.get('content') or '').encode('utf-8')).hexdigest()
    return {'strategy': PROMPT_VERSION, 'model': model, 'max_output_tokens': max_output_tokens, 'preferences': preferences,
            'brief': {key: row.get(key) for key in ('brief_id', 'revision', 'status', 'brief_json', 'approved_angle_index', 'approved_job_id', 'approved_topic_id')},
            'context': context, 'sources': sources}


def _run(db, run_id):
    row = db.execute('SELECT * FROM script_generation_runs WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        raise ValueError('剧本生成任务不存在')
    return dict(row)


def _attempt_count(db, run_id, index):
    return db.execute('SELECT COUNT(*) FROM script_generation_attempts WHERE run_id=? AND variant_index=?', (run_id, index)).fetchone()[0]


def _slots(db, run_id):
    return [dict(row) for row in db.execute('SELECT * FROM script_generation_slots WHERE run_id=? ORDER BY variant_index', (run_id,))]


def _int(value):
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def get_run(db, run_id, *, include_drafts=True):
    row = db.execute('''SELECT run_id,brief_id,brief_revision,angle_index,fingerprint,status,active_variant,error_code,error_message,
        recommended_index,recommendation_reason,unranked_reason,legacy_cached
        FROM script_generation_runs WHERE run_id=?''', (run_id,)).fetchone()
    if row is None:
        raise ValueError('剧本生成任务不存在')
    slots = _slots(db, run_id)
    completed = [slot for slot in slots if slot['status'] == 'completed']
    pending = next((slot for slot in slots if slot['status'] != 'completed'), None)
    usage = db.execute('SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) FROM script_generation_attempts WHERE run_id=?', (run_id,)).fetchone()
    drafts = []
    for slot in completed if include_drafts else ():
        draft = db.execute('SELECT * FROM script_drafts WHERE draft_id=?', (slot['draft_id'],)).fetchone()
        if draft:
            drafts.append({'draft_id': draft['draft_id'], 'variant_index': draft['variant_index'],
                           'status': draft['status'], 'script': json.loads(draft['script_json'])})
    resumable = row['status'] in ('queued', 'partial', 'failed', 'unknown')
    if pending and _attempt_count(db, run_id, pending['variant_index']) >= MAX_ATTEMPTS_PER_VARIANT:
        resumable = False
    state = {'run_id': run_id, 'brief_id': row['brief_id'], 'brief_revision': row['brief_revision'],
            'angle_index': row['angle_index'], 'status': row['status'],
            'completed_count': len(completed), 'target_count': len(slots) if row['legacy_cached'] else TARGET_COUNT,
            'active_variant': row['active_variant'], 'error_code': row['error_code'],
            'error_message': row['error_message'], 'can_resume': resumable,
            'requires_unknown_confirmation': row['status'] == 'unknown',
            'cached': bool(row['legacy_cached']), 'input_tokens': usage[0], 'output_tokens': usage[1],
            'recommendation_ready': row['recommended_index'] is not None,
            'recommended_index': row['recommended_index'],
            'recommendation_reason': row['recommendation_reason'] or '',
            'unranked_reason': row['unranked_reason'] or '', 'drafts': drafts,
            'request_fingerprint': row['fingerprint']}
    if not include_drafts:
        state.pop('drafts')
    return state


def list_runs(db, *, limit=50):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('limit 必须是 1 到 200 之间的整数')
    return [get_run(db, row[0], include_drafts=False) for row in db.execute(
        'SELECT run_id FROM script_generation_runs ORDER BY created_at DESC,run_id DESC LIMIT ?', (limit,))]


def _save_variant(db, row, index, variant, usage=None):
    snapshot = json.loads(row['snapshot_json'])
    variant = deepcopy(variant)
    variant['generation_preferences'] = snapshot['preferences']
    draft_id = 'SCRIPT-' + _hash({'request': row['fingerprint'], 'index': index, 'script': variant})[:16]
    db.execute('''INSERT INTO script_drafts(draft_id,brief_id,variant_index,status,request_fingerprint,
               script_json,recommended_index,recommendation_reason,input_tokens,output_tokens)
               VALUES(?,?,?,'candidate',?,?,-1,'',?,?)''',
               (draft_id, row['brief_id'], index, row['fingerprint'], _json(variant),
                _int((usage or {}).get('input_tokens')), _int((usage or {}).get('output_tokens'))))
    db.execute("UPDATE script_generation_slots SET status='completed',draft_id=?,draft_hash=? WHERE run_id=? AND variant_index=?",
               (draft_id, _hash(variant), row['run_id'], index))


def create_run(db, brief_id, preferences=None, model=None, *, max_output_tokens=MAX_OUTPUT_TOKENS):
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise ValueError('max_output_tokens 必须是 1 到 12000 之间的整数')
    ensure_schema(db)
    preferences = script_drafts.normalize_generation_preferences(preferences)
    model = model or doubao_llm.DEFAULT_MODEL
    with _transaction(db):
        snapshot = _snapshot(db, brief_id, preferences, model, max_output_tokens)
        fingerprint = _hash(snapshot)
        existing = db.execute('SELECT run_id FROM script_generation_runs WHERE fingerprint=?', (fingerprint,)).fetchone()
        if existing:
            result = get_run(db, existing['run_id'])
            result['cached'] = True
            return result
        if db.execute("SELECT 1 FROM script_drafts WHERE brief_id=? AND status='approved'", (brief_id,)).fetchone():
            raise ValueError('已有批准的剧本，不能覆盖或重新生成')
        run_id = 'SG-' + uuid.uuid4().hex
        angle_index = snapshot['brief']['approved_angle_index']
        if angle_index is None:
            angle_index = json.loads(snapshot['brief']['brief_json']).get('recommended_index', 0)
        db.execute('INSERT INTO script_generation_runs(run_id,brief_id,fingerprint,snapshot_json,brief_revision,angle_index) VALUES(?,?,?,?,?,?)',
                   (run_id, brief_id, fingerprint, _json(snapshot), snapshot['brief']['revision'] or 1, angle_index))
        for index in range(TARGET_COUNT):
            db.execute('INSERT INTO script_generation_slots(run_id,variant_index) VALUES(?,?)', (run_id, index))
        row = _run(db, run_id)
        # A legacy one-variant response remains a valid legacy cache entry. It is
        # not silently reinterpreted as a newly generated complete group.
        legacy_fingerprint = script_drafts._fingerprint(snapshot['context'], snapshot['sources'], preferences)
        legacy = db.execute('SELECT * FROM script_drafts WHERE brief_id=? AND request_fingerprint=? ORDER BY variant_index',
                            (brief_id, legacy_fingerprint)).fetchall() if model == doubao_llm.DEFAULT_MODEL else []
        if legacy:
            db.execute('DELETE FROM script_generation_slots WHERE run_id=?', (run_id,))
            for draft in legacy:
                if draft['variant_index'] in range(TARGET_COUNT):
                    db.execute("INSERT INTO script_generation_slots(run_id,variant_index,status,draft_id,draft_hash,draft_revision) VALUES(?,?,'completed',?,?,?)",
                               (run_id, draft['variant_index'], draft['draft_id'], _hash(json.loads(draft['script_json'])), draft['revision'] or 1))
            db.execute("UPDATE script_generation_runs SET status='completed',legacy_cached=1,recommendation_done=1,unranked_reason=? WHERE run_id=?",
                       ('沿用历史缓存；未执行新的 AI 推荐排序。', run_id))
        else:
            reconstruction = script_drafts._reconstruction_variant(snapshot['sources'])
            if reconstruction:
                _save_variant(db, row, 0, reconstruction)
    return get_run(db, run_id)


def _current(db, row):
    snapshot = json.loads(row['snapshot_json'])
    try:
        live = _snapshot(db, row['brief_id'], snapshot['preferences'], snapshot['model'], snapshot['max_output_tokens'])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return False
    if _hash(live) != row['fingerprint']:
        return False
    if db.execute("SELECT 1 FROM script_drafts WHERE brief_id=? AND status='approved'", (row['brief_id'],)).fetchone():
        return False
    for slot in _slots(db, row['run_id']):
        if slot['status'] != 'completed':
            continue
        draft = db.execute('SELECT status,script_json,revision FROM script_drafts WHERE draft_id=?', (slot['draft_id'],)).fetchone()
        if not draft or draft['status'] != 'candidate' or (draft['revision'] or 1) != slot['draft_revision'] or _hash(json.loads(draft['script_json'])) != slot['draft_hash']:
            return False
    return True


def _safe_error(error):
    """Only expose field identifiers/known error classes, never provider strings."""
    if isinstance(error, json.JSONDecodeError):
        return 'invalid_json', 'AI 返回的 JSON 语法无效；仅允许针对当前版本修复一次。'
    text = str(error)
    if 'source_id' in text:
        return 'invalid_source', 'source_ids 只能引用所提供的来源白名单，visual_requirements.source_ids 也必须遵守此规则。'
    fields = ('title', 'creative_thesis', 'beats', 'dialogue', 'speaker', 'visual_intent', 'characters',
              'camera_intent', 'reference_control', 'visual_requirements', 'must_preserve', 'may_adapt', 'beat_id',
              'scene', 'continuity_id', 'emotion', 'tone', 'pace', 'volume', 'pause', 'facial_expression',
              'gaze', 'gesture', 'body_action')
    field = next((name for name in fields if name in text), 'variant')
    return 'invalid_variant', f'{field}：当前完整版本缺少必要字段或字段不符合契约。'


def _normalize(raw, allowed):
    if not isinstance(raw, dict):
        raise ValueError('variant must be an object')
    # Explicitly reject grouped responses instead of discarding siblings.
    if set(raw) != {'variant'} or not isinstance(raw['variant'], dict):
        raise ValueError('variant: response must contain exactly one variant object')
    original = raw['variant']
    def validate_sources(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'source_ids':
                    if not isinstance(item, list) or any(not isinstance(identity, str) or identity not in allowed for identity in item):
                        raise ValueError('source_ids contains an unknown source_id')
                elif key == 'source_id':
                    if not isinstance(item, str) or item not in allowed:
                        raise ValueError('source_id is outside the source whitelist')
                else:
                    validate_sources(item)
        elif isinstance(value, list):
            for item in value:
                validate_sources(item)
    validate_sources(original)
    if not isinstance(original.get('beats'), list) or not original['beats']:
        raise ValueError('beats must be a nonempty list')
    for beat in original['beats']:
        if not isinstance(beat, dict):
            raise ValueError('beats must contain objects')
        if not isinstance(beat.get('source_ids'), list) or not beat['source_ids']:
            raise ValueError('source_ids must contain supplied evidence IDs')
        if any(not isinstance(value, str) or value not in allowed for value in beat['source_ids']):
            raise ValueError('source_ids contains an unknown source_id')
        for field in ('beat_id', 'camera_intent', 'scene', 'continuity_id', 'emotion', 'tone', 'pace',
                      'volume', 'pause', 'facial_expression', 'gaze', 'gesture', 'body_action'):
            if not isinstance(beat.get(field), str) or not beat[field].strip():
                raise ValueError(f'{field} is required')
        for field in ('must_preserve', 'may_adapt', 'visual_requirements'):
            if not isinstance(beat.get(field), list):
                raise ValueError(f'{field} must be an explicit list')
        if not isinstance(beat.get('reference_control'), dict):
            raise ValueError('reference_control is required')
        mode = script_drafts.normalize_speech_mode(beat.get('speech_mode'), speaker=beat.get('speaker', ''))
        declared = {str(item.get('id') or item.get('name')) for item in original.get('characters', []) if isinstance(item, dict)}
        # Visible identities are physical characters in every speech mode. Check
        # both aliases so the preferred alias cannot hide an invalid reference.
        for field in ('characters', 'cast'):
            if field in beat and (not isinstance(beat[field], list) or any(
                    not isinstance(item, str) or item not in declared for item in beat[field])):
                raise ValueError('characters must declare known visible performers')
        singular = beat.get('speaker') or ''
        plural = beat.get('speakers') or []
        if not isinstance(singular, str) or not isinstance(plural, list) or any(
                not isinstance(item, str) or not item.strip() for item in plural):
            raise ValueError('speaker and speakers must contain valid character IDs')
        singular = singular.strip()
        plural = [item.strip() for item in plural]
        if singular and plural and singular != plural[0]:
            raise ValueError('speaker must equal the first speakers ID')
        speakers = set(plural + ([singular] if singular else []))
        if not speakers:
            raise ValueError('speaker is required')
        if mode == 'narration':
            if any(speaker not in declared and script_drafts.normalize_speech_mode(
                    None, speaker=speaker) != 'narration' for speaker in speakers):
                raise ValueError('speaker must be a declared character or explicit narrator')
        else:
            if any(speaker not in declared for speaker in speakers):
                raise ValueError('speaker must belong to declared characters')
            if mode == 'on_screen':
                cast = beat.get('characters') or beat.get('cast') or []
                if not cast or any(speaker not in cast for speaker in speakers):
                    raise ValueError('speaker must belong to the visible characters')
    normalized = script_drafts._normalize_variant(original, allowed_source_ids=allowed)
    # Legacy normalization intentionally projects character/top-level fields.
    # Merge it over full authored objects here so director extensions survive.
    result = {**deepcopy(original), **normalized}
    if normalized.get('characters'):
        originals = {str(item.get('id') or item.get('name')): item for item in original.get('characters', []) if isinstance(item, dict)}
        result['characters'] = [{**deepcopy(originals.get(item['id'], {})), **item} for item in normalized['characters']]
    return result


def _prompt(db, row, index, previous_error):
    snapshot = json.loads(row['snapshot_json'])
    completed = [item['script'] for item in get_run(db, row['run_id'])['drafts']]
    if index == TARGET_COUNT:
        return ('RANKING_REQUEST: Compare the three complete scripts. Return only JSON '
                '{"recommended_index":0,"recommendation_reason":"specific evidence-based reason"}. '
                'recommended_index must be an integer from 0 to 2.\n'
                + _json([{'title': item.get('title'), 'creative_thesis': item.get('creative_thesis'),
                           'beats': [{key: beat[key] for key in ('dialogue', 'scene', 'body_action', 'visual_intent', 'source_ids', 'must_preserve') if key in beat}
                                     for beat in item.get('beats', [])]} for item in completed]))
    return script_drafts._single_variant_prompt(
        snapshot['context'], snapshot['sources'], snapshot['preferences'],
        variant_index=index, previous_variants=completed, repair_reason=previous_error)


def run_step(db, run_id, generate_json=doubao_llm.complete_json, external_io=lambda fn: fn()):
    """Execute at most one prepared provider request, and atomically check/save it."""
    _clean(db)
    with _transaction(db):
        row = _run(db, run_id)
        if row['status'] != 'queued':
            return get_run(db, run_id)
        if not _current(db, row):
            db.execute("UPDATE script_generation_runs SET status='stale',active_variant=NULL,error_code='stale_inputs',error_message=? WHERE run_id=?",
                       ('简报、来源或已保存剧本发生变化；当前快照不可继续生成。', run_id))
            return get_run(db, run_id)
        slots = _slots(db, run_id)
        pending = next((slot for slot in slots if slot['status'] != 'completed'), None)
        index = pending['variant_index'] if pending else TARGET_COUNT
        count = _attempt_count(db, run_id, index)
        limit = MAX_ATTEMPTS_PER_VARIANT if pending else 1
        if count >= limit:
            db.execute("UPDATE script_generation_runs SET status='failed',error_code='attempt_limit',active_variant=NULL WHERE run_id=?", (run_id,))
            return get_run(db, run_id)
        attempt_id = uuid.uuid4().hex
        prompt = _prompt(db, row, index, row['error_message'] if count else None)
        db.execute("INSERT INTO script_generation_attempts(attempt_id,run_id,variant_index,attempt_number,stage,status) VALUES(?,?,?,?,?,'in_flight')",
                   (attempt_id, run_id, index, count + 1, 'variant' if pending else 'recommendation'))
        if pending:
            db.execute("UPDATE script_generation_slots SET status='in_flight' WHERE run_id=? AND variant_index=?", (run_id, index))
        db.execute("UPDATE script_generation_runs SET status='running',active_variant=?,updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (index + 1 if pending else None, run_id))
    snapshot = json.loads(row['snapshot_json'])
    raw, usage, error, diagnostic = None, {}, None, {}
    validation_error = False
    try:
        raw, usage = external_io(lambda: generate_json(prompt, max_tokens=snapshot['max_output_tokens'] if pending else RECOMMENDATION_MAX_TOKENS, model=snapshot['model'], strict_json=True))
    except Exception as exc:
        error = exc
        diagnostic = getattr(exc, 'response_diagnostic', {}) or {}
        usage = diagnostic
    usage = usage if isinstance(usage, dict) else {}
    input_tokens, output_tokens = _int(usage.get('input_tokens')), _int(usage.get('output_tokens'))
    # Never retain raw error/prompt text in the durable diagnostics.
    diagnostic = {key: diagnostic[key] for key in ('kind', 'finish_reason', 'max_tokens', 'input_tokens', 'output_tokens', 'output_sha256', 'line', 'column') if key in diagnostic}
    with _transaction(db):
        latest = _run(db, run_id)
        attempt = db.execute('SELECT * FROM script_generation_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
        # Boot recovery or another explicit resolution can fence off this callback.
        db.execute('UPDATE script_generation_attempts SET input_tokens=?,output_tokens=?,diagnostic_json=?,finished_at=CURRENT_TIMESTAMP WHERE attempt_id=?',
                   (input_tokens, output_tokens, _json(diagnostic), attempt_id))
        if attempt['status'] != 'in_flight' or latest['status'] != 'running':
            return get_run(db, run_id)
        if not _current(db, latest):
            db.execute("UPDATE script_generation_attempts SET status='stale',error_code='stale_inputs' WHERE attempt_id=?", (attempt_id,))
            db.execute("UPDATE script_generation_runs SET status='stale',active_variant=NULL,error_code='stale_inputs',error_message=? WHERE run_id=?",
                       ('生成期间简报、来源或已保存剧本发生变化；本次结果未保存。', run_id))
            return get_run(db, run_id)
        if index == TARGET_COUNT:
            recommended, reason = None, ''
            if not error and isinstance(raw, dict):
                value = raw.get('recommended_index')
                if type(value) is int and value in range(TARGET_COUNT) and isinstance(raw.get('recommendation_reason'), str) and raw['recommendation_reason'].strip():
                    recommended, reason = value, raw['recommendation_reason'].strip()
            db.execute("UPDATE script_generation_attempts SET status=?,error_code=? WHERE attempt_id=?", ('completed' if recommended is not None else 'failed', None if recommended is not None else 'ranking_unavailable', attempt_id))
            db.execute("UPDATE script_generation_runs SET status='completed',active_variant=NULL,recommendation_done=1,recommended_index=?,recommendation_reason=?,unranked_reason=?,error_code=NULL,error_message=NULL WHERE run_id=?",
                       (recommended, reason, '' if recommended is not None else 'AI 推荐暂不可用；三版剧本均已保存，可直接审阅。', run_id))
            if recommended is not None:
                db.execute("UPDATE script_drafts SET recommended_index=?,recommendation_reason=? WHERE brief_id=? AND request_fingerprint=? AND status='candidate'",
                           (recommended, reason, latest['brief_id'], latest['fingerprint']))
            return get_run(db, run_id)
        code, message, status = None, None, None
        if error is None:
            try:
                variant = _normalize(raw, {item['source_id'] for item in snapshot['sources']})
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                error = exc
                validation_error = True
        if error is None:
            _save_variant(db, latest, index, variant, {'input_tokens': input_tokens, 'output_tokens': output_tokens})
            db.execute("UPDATE script_generation_attempts SET status='completed' WHERE attempt_id=?", (attempt_id,))
            db.execute("UPDATE script_generation_runs SET status='queued',active_variant=NULL,error_code=NULL,error_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,))
            return get_run(db, run_id)
        if diagnostic.get('kind') == 'truncated' or diagnostic.get('finish_reason') in ('length', 'max_tokens'):
            code, message, status = 'output_truncated', 'AI 输出达到长度上限，未保存不完整剧本。手动继续时将使用精简的单版本提示。', 'failed'
        elif (validation_error or isinstance(error, json.JSONDecodeError)) and diagnostic.get('kind') != 'response_json':
            code, message = _safe_error(error)
            status = 'queued' if count + 1 < MAX_ATTEMPTS_PER_VARIANT else 'failed'
        else:
            code, message, status = 'transport_unknown', '模型请求结果未知。继续前需明确确认可能重复调用并产生费用。', 'unknown'
        db.execute('UPDATE script_generation_attempts SET status=?,error_code=?,error_message=? WHERE attempt_id=?', ('unknown' if status == 'unknown' else 'failed', code, message, attempt_id))
        db.execute('UPDATE script_generation_slots SET status=? WHERE run_id=? AND variant_index=?', ('unknown' if status == 'unknown' else 'pending', run_id, index))
        db.execute('UPDATE script_generation_runs SET status=?,active_variant=NULL,error_code=?,error_message=?,updated_at=CURRENT_TIMESTAMP WHERE run_id=?', (status, code, message, run_id))
    return get_run(db, run_id)


def resume_run(db, run_id, confirm_unknown=False):
    _clean(db)
    with _transaction(db):
        row = _run(db, run_id)
        state = get_run(db, run_id)
        if row['status'] == 'unknown' and not confirm_unknown:
            raise ValueError('请求结果未知；继续前必须明确传入 confirm_unknown=true')
        if row['status'] in ('completed', 'running', 'stale'):
            return state
        if not _current(db, row):
            db.execute("UPDATE script_generation_runs SET status='stale',error_code='stale_inputs',active_variant=NULL WHERE run_id=?", (run_id,))
            return get_run(db, run_id)
        if not state['can_resume']:
            raise ValueError('当前版本已达到两次尝试上限，不能继续本任务')
        db.execute("UPDATE script_generation_slots SET status='pending' WHERE run_id=? AND status<>'completed'", (run_id,))
        db.execute("UPDATE script_generation_runs SET status='queued',active_variant=NULL,updated_at=CURRENT_TIMESTAMP WHERE run_id=?", (run_id,))
    return get_run(db, run_id)


def recover_interrupted(db):
    """Boot-only recovery under the server's exclusive runtime lock; no IO."""
    ensure_schema(db)
    with _transaction(db):
        ids = [row[0] for row in db.execute("SELECT DISTINCT run_id FROM script_generation_attempts WHERE status='in_flight'")]
        for run_id in ids:
            db.execute("UPDATE script_generation_attempts SET status='unknown',error_code='interrupted',error_message='请求准备完成后进程中断。' WHERE run_id=? AND status='in_flight'", (run_id,))
            db.execute("UPDATE script_generation_slots SET status='unknown' WHERE run_id=? AND status='in_flight'", (run_id,))
            db.execute("UPDATE script_generation_runs SET status='unknown',active_variant=NULL,error_code='interrupted',error_message='进程中断，模型请求结果未知；继续前需明确确认。' WHERE run_id=?", (run_id,))
            _finish_interrupted_ranking(db, run_id)
    return list_runs(db)


def interrupt_run(db, run_id):
    """Fence one unexpected worker failure without affecting concurrent runs."""
    _clean(db)
    with _transaction(db):
        row = _run(db, run_id)
        if row['status'] in ('completed', 'stale', 'unknown', 'failed'):
            return get_run(db, run_id)
        db.execute("UPDATE script_generation_attempts SET status='unknown',error_code='interrupted',error_message='请求准备完成后执行器中断。' WHERE run_id=? AND status='in_flight'", (run_id,))
        db.execute("UPDATE script_generation_slots SET status='unknown' WHERE run_id=? AND status='in_flight'", (run_id,))
        db.execute("UPDATE script_generation_runs SET status='unknown',active_variant=NULL,error_code='interrupted',error_message='执行器停止，请求结果未知；继续前需明确确认。' WHERE run_id=?", (run_id,))
        _finish_interrupted_ranking(db, run_id)
    return get_run(db, run_id)


def _finish_interrupted_ranking(db, run_id):
    """Ranking is optional: interrupted ranking cannot trap three saved scripts."""
    completed = db.execute("SELECT COUNT(*) FROM script_generation_slots WHERE run_id=? AND status='completed'", (run_id,)).fetchone()[0]
    if completed == TARGET_COUNT:
        db.execute("UPDATE script_generation_runs SET status='completed',active_variant=NULL,recommendation_done=1,recommended_index=NULL,recommendation_reason='',unranked_reason=?,error_code=NULL,error_message=NULL WHERE run_id=?",
                   ('AI 推荐在执行中中断；三版剧本均已保存，可直接审阅。', run_id))
