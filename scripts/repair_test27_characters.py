"""Repair the user-confirmed four-person Test27 script before any generation."""
import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from factory import creative_ir, production_compiler, script_drafts
from factory.steps import s1_storyboard

JOB = 'J-TOPIC-2eda8a6ac7beaed8'
DRAFT = 'SCRIPT-60eab959892c2e5b'
EXPECTED = {'narration', 'eng_pur', 'event_cta', 'admin', 'rent_sale1', 'rent_sale2', 'rent_sale3'}
VISIBLE = {'eng_pur', 'event_cta', 'admin', 'rent_sale1'}
NOTE = '用户确认保留三类客户及同一业务员，共4个出镜角色；沿用rent_sale1形象，旁白不定妆。'

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

def prepare(c):
    job = c.execute('SELECT * FROM jobs WHERE job_id=?', (JOB,)).fetchone()
    draft = c.execute('SELECT * FROM script_drafts WHERE draft_id=?', (DRAFT,)).fetchone()
    if not job or job['state'] != 'STORYBOARDED' or job['cost_cents'] or job['production_worker_pid'] or job['production_spec_hash']:
        raise ValueError('task no longer matches the unpaid, unconfigured incident')
    if not draft or draft['approved_job_id'] != JOB or draft['status'] != 'approved':
        raise ValueError('approved script changed')
    for table in ('shots', 'production_nodes', 'production_edges', 'provider_calls', 'production_node_events', 'character_identity_anchors', 'scene_visual_anchors'):
        if c.execute(f'SELECT 1 FROM {table} WHERE job_id=?', (JOB,)).fetchone():
            raise ValueError(f'production evidence exists in {table}; preserve and reassess')
    original = json.loads(draft['script_json'])
    if {x['id'] for x in original['characters']} != EXPECTED:
        raise ValueError('script identities changed or repair already applied')
    def rewrite(value):
        if isinstance(value, str):
            return re.sub(r'\brent_sale[23]\b', 'rent_sale1', value)
        if isinstance(value, list):
            rewritten = [rewrite(x) for x in value]
            return list(dict.fromkeys(rewritten)) if all(isinstance(x, str) for x in rewritten) else rewritten
        if isinstance(value, dict):
            return {k: rewrite(v) for k, v in value.items()}
        return value
    edited = deepcopy(original)
    edited['characters'] = [x for x in edited['characters'] if x['id'] not in {'rent_sale2','rent_sale3'}]
    edited = rewrite(edited)
    next(x for x in edited['characters'] if x['id']=='rent_sale1')['background_description'] = '同一名设备租赁公司业务员，在三场分别对接工程、活动和企业办公客户，身份、服装与声线保持一致。'
    edited.setdefault('generation_preferences', {})['suggested_character_count'] = 4
    for before, after in zip(original['beats'], edited['beats']):
        for key in ('dialogue','duration_sec','visual_intent','speech_mode'):
            if before.get(key) != after.get(key):
                raise ValueError(f'unrequested editorial change: {key}')
    board = script_drafts._storyboard(edited)
    s1_storyboard._validate(board)
    if len(board['masters']) != len(json.loads(job['storyboard'])['masters']):
        raise ValueError('unexpected change in production unit count')
    if {x['id'] for x in board['characters']} != VISIBLE:
        raise ValueError('visible cast is not exactly the confirmed four people')
    ir = creative_ir.from_storyboard(board)
    compiled = production_compiler.compile_ir(ir, job_id=JOB, budget_cents=2500,
        capabilities=production_compiler.ProviderCapabilities.seedance_2_5(), runtime_storyboard=board)
    if not compiled.compilable or {x['id'] for x in compiled.spec.characters} != VISIBLE:
        raise ValueError(f'four-person production compilation failed: {compiled.blockers}')
    signature = hashlib.sha256(encode([dict(job),dict(draft)]).encode()).hexdigest()
    return signature, draft, edited, board, ir

def repair(c, expected):
    c.execute('BEGIN IMMEDIATE')
    try:
        signature, draft, edited, board, ir = prepare(c)
        if signature != expected:
            raise ValueError('incident changed after rehearsal')
        old_payload = json.loads(draft['script_json'])
        revision = int(draft['revision'] or 1)
        encoded = encode(old_payload)
        revision_id = 'REV-' + hashlib.sha256(f'script:{DRAFT}:{revision}:{encoded}'.encode()).hexdigest()[:20]
        c.execute('INSERT OR IGNORE INTO content_revisions(revision_id,entity_type,entity_id,revision,payload_json,change_note) VALUES(?,?,?,?,?,?)',
                  (revision_id, 'script', DRAFT, revision, encoded, NOTE))
        c.execute('UPDATE script_drafts SET script_json=?,storyboard_json=?,revision=?,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?',
                  (encode(edited), encode(board), revision+1, DRAFT))
        c.execute('UPDATE jobs SET storyboard=?,creative_ir_json=?,creative_ir_version=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?',
                  (encode(board), encode(ir.as_dict()), ir.version, JOB))
        c.execute('INSERT INTO events(job_id,step,level,message) VALUES(?,?,?,?)', (JOB, 'character_identity_repair', 'info', NOTE))
        c.commit()
    except BaseException:
        c.rollback()
        raise

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    database = args.database.resolve()
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as source, tempfile.TemporaryDirectory() as td:
        with closing(sqlite3.connect(Path(td)/'rehearsal.db')) as copy:
            source.backup(copy); copy.row_factory = sqlite3.Row
            expected, _, _, board, _ = prepare(copy)
            repair(copy, expected)
            try:
                repair(copy, expected)
            except ValueError:
                pass
            else:
                raise ValueError('repeated repair unexpectedly succeeded')
        backup = None
        if args.apply:
            backup = database.parent/'backups'/('test27-identity-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))/'factory.db'
            backup.parent.mkdir(parents=True)
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            with closing(sqlite3.connect(database)) as live:
                live.row_factory = sqlite3.Row
                repair(live, expected)
        print(json.dumps({'applied':args.apply,'visible_characters':[x['name'] for x in board['characters']],
            'masters':len(board['masters']),'state':'STORYBOARDED','cost_cents':0,'paid_submissions':0,
            'backup':str(backup) if backup else None,'repeat_repair_rejected':True},ensure_ascii=True))

if __name__ == '__main__':
    main()
