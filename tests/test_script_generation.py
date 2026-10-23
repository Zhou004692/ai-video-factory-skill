"""Offline tests for durable, single-variant generation."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import sys
import unittest
import os
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from factory import db, script_sources, script_drafts
from factory import script_generation as engine


def variant(source_id, title='Version'):
    return {'title': title, 'creative_thesis': 'Keep evidence', 'director_notes': {'lens': 'wide'},
            'characters': [{'id': 'a', 'name': 'A', 'look': 'workwear', 'custom': 'preserved'}],
            'beats': [{'beat_id': 'b1', 'speaker': 'a', 'dialogue': 'A complete line',
                       'duration_sec': 3, 'visual_intent': 'A walks into warehouse',
                       'scene': 'Warehouse in daylight', 'continuity_id': 'warehouse-day', 'characters': ['a'],
                       'source_ids': [source_id], 'camera_intent': 'track A',
                       'must_preserve': ['warehouse'], 'may_adapt': ['angle'],
                       'visual_requirements': [{'id': 'r1', 'description': 'walking', 'source_ids': [source_id]}],
                       'reference_control': {'choice': 'text', 'rationale': 'sufficient'},
                       **{field: 'explicit' for field in script_drafts.PERFORMANCE_TEXT_FIELDS}}]}


class ScriptGenerationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(db.DDL)
        self.source_id = script_sources.ingest_text(self.db, 'Reliable facts', title='Source').source_id
        self.brief_id = 'BRIEF-administrative'
        self.db.execute('INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,approved_angle_index,approved_job_id) VALUES(?,?,?,?,?,?)',
                        (self.brief_id, 'approved', 'Title', json.dumps({'audience': 'Audience', 'angles': [{'title': 'Creative angle'}], 'recommended_index': 0}), 0, 'J-administrative'))
        self.db.execute('INSERT INTO topic_brief_sources(brief_id,source_id) VALUES(?,?)', (self.brief_id, self.source_id))
        self.db.commit()
        engine.ensure_schema(self.db)
        self.calls = []

    def tearDown(self):
        self.db.close()

    def generate(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if 'recommended_index' in prompt and 'RANKING_REQUEST' in prompt:
            return {'recommended_index': 2, 'recommendation_reason': 'Clear evidence'}, {'input_tokens': 2, 'output_tokens': 3}
        return {'variant': variant(self.source_id, f'Version {len(self.calls)}')}, {'input_tokens': 10, 'output_tokens': 20}

    def step(self, state, generate=None):
        return engine.run_step(self.db, state['run_id'], generate_json=generate or self.generate)

    def test_one_variant_per_request_preserves_all_director_fields(self):
        state = engine.create_run(self.db, self.brief_id)
        state = self.step(state)
        self.assertEqual(state['completed_count'], 1)
        saved = state['drafts'][0]['script']
        self.assertEqual(saved['director_notes'], {'lens': 'wide'})
        self.assertEqual(saved['characters'][0]['custom'], 'preserved')
        self.assertEqual(saved['beats'][0]['reference_control']['choice'], 'text')
        self.assertEqual(saved['beats'][0]['emotion'], 'explicit')
        prompt, kwargs = self.calls[0]
        self.assertNotIn(self.brief_id, prompt)
        self.assertNotIn('J-administrative', prompt)
        self.assertIn(self.source_id, prompt)
        self.assertEqual(kwargs['max_tokens'], 12000)
        self.assertFalse(self.db.in_transaction)

    def test_rejects_administrative_source_and_only_one_targeted_repair(self):
        state = engine.create_run(self.db, self.brief_id)
        bad = lambda *a, **k: ({'variant': variant(self.brief_id)}, {'input_tokens': 1, 'output_tokens': 2})
        state = self.step(state, bad)
        self.assertEqual(state['completed_count'], 0)
        self.assertEqual(state['status'], 'queued')
        self.assertEqual(state['error_code'], 'invalid_source')
        state = self.step(state, bad)
        self.assertEqual(state['status'], 'failed')
        self.assertFalse(state['can_resume'])
        self.assertEqual(state['input_tokens'], 2)
        self.assertEqual(len(state['drafts']), 0)

    def test_two_saved_survive_unknown_then_resume_only_third(self):
        state = engine.create_run(self.db, self.brief_id)
        state = self.step(self.step(state))
        def fail(*a, **k):
            raise TimeoutError('secret transport response')
        state = self.step(state, fail)
        self.assertEqual(state['status'], 'unknown')
        self.assertEqual(state['completed_count'], 2)
        self.assertNotIn('secret', state['error_message'])
        with self.assertRaises(ValueError):
            engine.resume_run(self.db, state['run_id'])
        state = engine.resume_run(self.db, state['run_id'], confirm_unknown=True)
        state = self.step(state)
        self.assertEqual(state['completed_count'], 3)
        state = self.step(state)
        self.assertEqual(state['status'], 'completed')
        self.assertTrue(state['recommendation_ready'])
        self.assertEqual(state['recommended_index'], 2)
        self.assertEqual(len(self.calls), 4)

    def test_cache_is_partial_and_snapshot_sensitive(self):
        state = engine.create_run(self.db, self.brief_id, model='m1')
        self.step(state)
        cached = engine.create_run(self.db, self.brief_id, model='m1')
        self.assertEqual(cached['run_id'], state['run_id'])
        self.assertEqual(cached['completed_count'], 1)
        self.assertTrue(cached['cached'])
        self.assertNotEqual(engine.create_run(self.db, self.brief_id, model='m2')['run_id'], state['run_id'])
        self.assertNotEqual(engine.create_run(self.db, self.brief_id, model='m1', preferences={'suggested_shot_count': 6})['run_id'], state['run_id'])
        self.db.execute('UPDATE topic_briefs SET revision=revision+1 WHERE brief_id=?', (self.brief_id,))
        self.db.commit()
        self.assertNotEqual(engine.create_run(self.db, self.brief_id, model='m1')['run_id'], state['run_id'])

    def test_dispatch_identity_persisted_no_duplicate_inflight(self):
        state = engine.create_run(self.db, self.brief_id)
        def callback(prompt, **kwargs):
            observed = engine.get_run(self.db, state['run_id'])
            self.assertEqual(observed['status'], 'running')
            self.assertFalse(self.db.in_transaction)
            nested = self.step(state)
            self.assertEqual(nested['status'], 'running')
            return self.generate(prompt, **kwargs)
        result = self.step(state, callback)
        self.assertEqual(result['completed_count'], 1)
        self.assertEqual(len(self.calls), 1)

    def test_stale_response_after_brief_or_source_mutation_not_saved(self):
        for field in ('brief', 'source'):
            with self.subTest(field=field):
                state = engine.create_run(self.db, self.brief_id, model=field)
                def callback(*a, **kw):
                    if field == 'brief':
                        self.db.execute('UPDATE topic_briefs SET revision=revision+1 WHERE brief_id=?', (self.brief_id,))
                    else:
                        self.db.execute('UPDATE script_sources SET content=content || ? WHERE source_id=?', ('edited', self.source_id))
                    self.db.commit()
                    return {'variant': variant(self.source_id)}, {'input_tokens': 4, 'output_tokens': 5}
                state = self.step(state, callback)
                self.assertEqual(state['status'], 'stale')
                self.assertEqual(state['completed_count'], 0)
                self.assertEqual(state['input_tokens'], 4)

    def test_recovery_marks_inflight_unknown_without_redispatch(self):
        state = engine.create_run(self.db, self.brief_id)
        def crash(*a, **kw):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.step(state, crash)
        engine.recover_interrupted(self.db)
        recovered = engine.get_run(self.db, state['run_id'])
        self.assertEqual(recovered['status'], 'unknown')
        self.assertTrue(recovered['requires_unknown_confirmation'])
        self.assertEqual(self.step(state)['status'], 'unknown')
        self.assertFalse(self.calls)

    def test_ranking_interruption_finishes_unranked_without_deadend_or_retry(self):
        state = engine.create_run(self.db, self.brief_id)
        for _ in range(3):
            state = self.step(state)
        def crash(*a, **kw):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.step(state, crash)
        engine.recover_interrupted(self.db)
        state = engine.get_run(self.db, state['run_id'])
        self.assertEqual(state['status'], 'completed')
        self.assertFalse(state['recommendation_ready'])
        self.assertTrue(state['unranked_reason'])
        self.assertFalse(state['can_resume'])
        self.assertEqual(len(self.calls), 3)

    def test_truncation_counts_failed_usage_and_stops_without_auto_retry(self):
        state = engine.create_run(self.db, self.brief_id)
        def truncated(*a, **k):
            error = json.JSONDecodeError('partial', '{', 1)
            error.response_diagnostic = {'kind': 'truncated', 'input_tokens': 12, 'output_tokens': 12000, 'finish_reason': 'length'}
            raise error
        state = self.step(state, truncated)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['error_code'], 'output_truncated')
        self.assertEqual(state['input_tokens'], 12)
        self.assertEqual(state['output_tokens'], 12000)
        self.assertEqual(state['drafts'], [])
        self.assertEqual(self.step(state)['status'], 'failed')

    def test_strict_single_variant_never_silently_accepts_batch_or_partial(self):
        for raw in ({'variants': [variant(self.source_id), variant(self.source_id)]}, {'variant': {'title': 'incomplete'}}):
            state = engine.create_run(self.db, self.brief_id, model=json.dumps(raw))
            state = self.step(state, lambda *a, **k: (raw, {}))
            self.assertEqual(state['completed_count'], 0)
            self.assertEqual(state['drafts'], [])

    def test_recommendation_failure_does_not_block_completed_variants(self):
        state = engine.create_run(self.db, self.brief_id)
        for _ in range(3):
            state = self.step(state)
        state = self.step(state, lambda *a, **k: ({'recommended_index': 100}, {}))
        self.assertEqual(state['status'], 'completed')
        self.assertFalse(state['recommendation_ready'])
        self.assertIsNone(state['recommended_index'])
        self.assertTrue(state['unranked_reason'])

    def test_missing_direction_or_unknown_performer_is_repaired_not_saved(self):
        for field in ('camera_intent', 'emotion', 'scene', 'characters', 'reference_control'):
            with self.subTest(field=field):
                state = engine.create_run(self.db, self.brief_id, model=field)
                incomplete = variant(self.source_id)
                incomplete['beats'][0].pop(field)
                result = self.step(state, lambda *a, **k: ({'variant': incomplete}, {}))
                self.assertEqual(result['completed_count'], 0)
                self.assertEqual(result['status'], 'queued')
                repaired = self.step(result)
                self.assertEqual(repaired['completed_count'], 1)
        state = engine.create_run(self.db, self.brief_id, model='unknown-cast')
        invalid = variant(self.source_id)
        invalid['beats'][0]['speaker'] = 'undeclared'
        self.assertEqual(self.step(state, lambda *a, **k: ({'variant': invalid}, {}))['completed_count'], 0)

    def test_offscreen_speakers_must_be_declared_but_need_not_be_visible(self):
        for references in ({'speaker': 'undeclared'},
                           {'speaker': 'a', 'speakers': ['undeclared']},
                           {'speaker': 'undeclared', 'speakers': ['a']}):
            with self.subTest(references=references):
                state = engine.create_run(self.db, self.brief_id, model=json.dumps(references))
                invalid = variant(self.source_id)
                invalid['beats'][0].update({'speech_mode': 'off_screen', 'characters': [], **references})
                state = self.step(state, lambda *a, **k: ({'variant': invalid}, {}))
                self.assertEqual(state['completed_count'], 0)
                self.assertEqual(state['status'], 'queued')
                self.assertIn('speaker', state['error_message'])
        state = engine.create_run(self.db, self.brief_id, model='declared-offscreen')
        valid = variant(self.source_id)
        valid['beats'][0].update({'speech_mode': 'off_screen', 'characters': []})
        state = self.step(state, lambda *a, **k: ({'variant': valid}, {}))
        self.assertEqual(state['completed_count'], 1)
        self.assertEqual(state['drafts'][0]['script']['beats'][0]['speaker'], 'a')

    def test_narration_does_not_require_an_additional_physical_character(self):
        state = engine.create_run(self.db, self.brief_id)
        narrated = variant(self.source_id)
        narrated['beats'][0].update({'speech_mode': 'narration', 'speaker': 'narrator', 'characters': ['a']})
        state = self.step(state, lambda *a, **k: ({'variant': narrated}, {}))
        self.assertEqual(state['completed_count'], 1)
        self.assertEqual([character['id'] for character in state['drafts'][0]['script']['characters']], ['a'])

    def test_visible_cast_must_be_declared_in_every_speech_mode(self):
        for mode in ('on_screen', 'off_screen', 'narration'):
            for field in ('characters', 'cast'):
                with self.subTest(mode=mode, field=field):
                    state = engine.create_run(self.db, self.brief_id, model=f'cast-{mode}-{field}')
                    invalid = variant(self.source_id)
                    invalid['beats'][0].update({'speech_mode': mode, field: ['undeclared']})
                    if mode == 'narration':
                        invalid['beats'][0]['speaker'] = 'narrator'
                    state = self.step(state, lambda *a, **k: ({'variant': invalid}, {}))
                    self.assertEqual(state['completed_count'], 0)
                    self.assertEqual(state['status'], 'queued')

    def test_speaker_alias_conflicts_are_rejected_before_normalization(self):
        for mode in ('on_screen', 'off_screen'):
            for speaker in ('undeclared', 'b'):
                with self.subTest(mode=mode, speaker=speaker):
                    state = engine.create_run(self.db, self.brief_id, model=f'alias-{mode}-{speaker}')
                    invalid = variant(self.source_id)
                    invalid['characters'].append({'id': 'b', 'name': 'B'})
                    invalid['beats'][0].update({'speech_mode': mode, 'speaker': speaker,
                                               'speakers': ['a'], 'characters': ['a', 'b']})
                    state = self.step(state, lambda *a, **k: ({'variant': invalid}, {}))
                    self.assertEqual(state['completed_count'], 0)
                    self.assertEqual(state['status'], 'queued')

    def test_narration_mode_does_not_allow_arbitrary_unknown_person_identity(self):
        state = engine.create_run(self.db, self.brief_id)
        invalid = variant(self.source_id)
        invalid['beats'][0].update({'speech_mode': 'narration', 'speaker': 'undeclared-person'})
        state = self.step(state, lambda *a, **k: ({'variant': invalid}, {}))
        self.assertEqual(state['completed_count'], 0)
        self.assertEqual(state['status'], 'queued')

    def test_management_listing_is_bounded_and_does_not_load_full_scripts(self):
        state = self.step(engine.create_run(self.db, self.brief_id, model='first'))
        engine.create_run(self.db, self.brief_id, model='second')
        listed = engine.list_runs(self.db, limit=1)
        self.assertEqual(len(listed), 1)
        self.assertNotIn('drafts', listed[0])
        self.assertEqual(listed[0]['brief_revision'], 1)
        self.assertEqual(listed[0]['angle_index'], 0)

    def test_reconstruction_is_saved_losslessly_without_a_model_request(self):
        analysis = {'segments': [{'start_sec': 0, 'end_sec': 4, 'role': 'hook',
                    'scene': 'A warehouse', 'speaker': 'a', 'characters': ['a'],
                    'dialogue': 'Original observed words', 'action': 'Walking',
                    'camera': 'Tracking', 'emotion': 'Focused'}]}
        self.db.execute('UPDATE script_sources SET source_type=?,metadata_json=? WHERE source_id=?',
                        ('video', json.dumps({'structure_analysis': analysis}), self.source_id))
        self.db.commit()
        state = engine.create_run(self.db, self.brief_id)
        self.assertEqual(state['completed_count'], 1)
        self.assertEqual(state['drafts'][0]['script']['beats'][0]['dialogue'], 'Original observed words')
        self.assertEqual(state['drafts'][0]['script']['beats'][0]['emotion'], 'Focused')
        self.assertEqual(state['input_tokens'], 0)
        self.assertFalse(self.calls)

    def test_approval_during_callback_rejects_late_result_and_preserves_draft(self):
        state = self.step(engine.create_run(self.db, self.brief_id))
        saved_id = state['drafts'][0]['draft_id']
        def approve(*a, **kw):
            self.db.execute("UPDATE script_drafts SET status='approved' WHERE draft_id=?", (saved_id,))
            self.db.commit()
            return {'variant': variant(self.source_id)}, {'input_tokens': 3, 'output_tokens': 4}
        state = self.step(state, approve)
        self.assertEqual(state['status'], 'stale')
        self.assertEqual(state['completed_count'], 1)
        self.assertEqual(state['drafts'][0]['status'], 'approved')

    def test_deleted_brief_during_callback_is_stale_without_orphan_insert(self):
        state = engine.create_run(self.db, self.brief_id)
        def delete(*a, **kw):
            self.db.execute('DELETE FROM topic_brief_sources WHERE brief_id=?', (self.brief_id,))
            self.db.execute('DELETE FROM topic_briefs WHERE brief_id=?', (self.brief_id,))
            self.db.commit()
            return {'variant': variant(self.source_id)}, {}
        state = self.step(state, delete)
        self.assertEqual(state['status'], 'stale')
        self.assertEqual(state['drafts'], [])

    def test_same_text_revision_edit_of_saved_draft_stales_run(self):
        state = self.step(engine.create_run(self.db, self.brief_id))
        self.db.execute('UPDATE script_drafts SET revision=revision+1 WHERE draft_id=?', (state['drafts'][0]['draft_id'],))
        self.db.commit()
        state = self.step(state)
        self.assertEqual(state['status'], 'stale')
        self.assertEqual(len(self.calls), 1)

    def test_legacy_single_cached_variant_keeps_its_actual_target(self):
        context, sources = script_drafts._load_context(self.db, self.brief_id)
        fingerprint = script_drafts._fingerprint(context, sources)
        self.db.execute('INSERT INTO script_drafts(draft_id,brief_id,variant_index,request_fingerprint,script_json) VALUES(?,?,?,?,?)',
                        ('legacy-script', self.brief_id, 0, fingerprint, json.dumps(variant(self.source_id))))
        self.db.commit()
        state = engine.create_run(self.db, self.brief_id)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['completed_count'], 1)
        self.assertEqual(state['target_count'], 1)
        self.assertFalse(state['recommendation_ready'])
        self.assertEqual(self.step(state)['status'], 'completed')
        self.assertFalse(self.calls)

    def test_single_prompt_has_no_batch_contract_or_group_instruction(self):
        state = self.step(engine.create_run(self.db, self.brief_id))
        prompt = self.calls[0][0]
        self.assertNotIn('"variants": [', prompt)
        self.assertIn('"variant": {', prompt)
        self.assertNotIn('"source_ids": ["SRC-..."]', prompt)
        self.assertNotIn('\u76ee\u6807\u751f\u6210\u4e09\u7248', prompt)

    def test_prompt_removes_administrative_fields_without_rewriting_source_facts(self):
        fact = 'The supplied source literally says BRIEF-administrative and J-administrative.'
        self.db.execute('UPDATE script_sources SET content=? WHERE source_id=?', (fact, self.source_id))
        self.db.commit()
        self.step(engine.create_run(self.db, self.brief_id))
        prompt = self.calls[0][0]
        self.assertIn(fact, prompt)
        self.assertNotIn('"brief_id":', prompt)
        self.assertNotIn('"approved_job_id":', prompt)

    def test_interrupt_only_fences_selected_run(self):
        first = engine.create_run(self.db, self.brief_id, model='first')
        second = engine.create_run(self.db, self.brief_id, model='second')
        def interrupt(prompt, **kwargs):
            result = engine.interrupt_run(self.db, first['run_id'])
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(engine.get_run(self.db, second['run_id'])['status'], 'queued')
            return self.generate(prompt, **kwargs)
        result = self.step(first, interrupt)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['completed_count'], 0)
        self.assertEqual(result['input_tokens'], 10)

    def test_edited_draft_stops_next_call_without_overwriting(self):
        state = self.step(engine.create_run(self.db, self.brief_id))
        draft = state['drafts'][0]
        edited = draft['script']
        edited['title'] = 'Human edit'
        self.db.execute('UPDATE script_drafts SET script_json=?,revision=revision+1 WHERE draft_id=?', (json.dumps(edited), draft['draft_id']))
        self.db.commit()
        state = self.step(state)
        self.assertEqual(state['status'], 'stale')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(state['drafts'][0]['script']['title'], 'Human edit')

    def test_maximum_lifetime_attempts_and_token_configuration(self):
        state = engine.create_run(self.db, self.brief_id, max_output_tokens=8000)
        self.assertNotEqual(state['run_id'], engine.create_run(self.db, self.brief_id)['run_id'])
        for _ in range(3):
            state = self.step(state, lambda *a, **k: ({'variant': {'title': 'broken'}}, {'input_tokens': 2}))
        self.assertEqual(state['status'], 'failed')
        count = self.db.execute('SELECT COUNT(*) FROM script_generation_attempts WHERE run_id=?', (state['run_id'],)).fetchone()[0]
        self.assertEqual(count, 2)
        with self.assertRaises(ValueError):
            engine.resume_run(self.db, state['run_id'])
        with self.assertRaises(ValueError):
            engine.create_run(self.db, self.brief_id, max_output_tokens=12001)

    def test_strict_client_does_not_close_incomplete_json_even_on_stop(self):
        from factory.clients import doubao_llm
        class Response:
            status_code = 200
            def json(self):
                return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"variant":{"title":"partial"'}}],
                        'usage': {'prompt_tokens': 7, 'completion_tokens': 8}}
        class Client:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def post(self, *a, **kw):
                return Response()
        with patch.dict(os.environ, {'ARK_API_KEY': 'private-key'}), patch.object(doubao_llm.httpx, 'Client', return_value=Client()):
            with self.assertRaises(json.JSONDecodeError) as raised:
                doubao_llm.complete_json('private-prompt', strict_json=True)
        self.assertEqual(raised.exception.response_diagnostic['input_tokens'], 7)
        self.assertEqual(raised.exception.response_diagnostic['output_tokens'], 8)

    def test_rejects_transaction_ownership_without_committing_callers_work(self):
        self.db.execute('UPDATE topic_briefs SET recommended_title=? WHERE brief_id=?', ('pending', self.brief_id))
        with self.assertRaises(RuntimeError):
            engine.create_run(self.db, self.brief_id)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()

if __name__ == '__main__':
    unittest.main()
