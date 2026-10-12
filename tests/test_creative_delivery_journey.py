"""Actual approval hand-off: controlled tags become immutable production intent."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from factory import db, creative_learning, full_flow_regression, production_contract, script_drafts, topic_briefs
from factory.management_api import ManagementService

TAGS_A = dict(hook_type='question', narrative_pattern='process_demo', proof_type='visual_demo', cta_type='save')
TAGS_B = dict(hook_type='result_first', narrative_pattern='comparison', proof_type='data_point', cta_type='follow')


class CreativeProductionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = sqlite3.connect(':memory:')
        self.database.row_factory = sqlite3.Row
        self.database.executescript(db.DDL)
        self.addCleanup(self.database.close)
        service = ManagementService(self.database, self.root, worker_mode='external')
        source = service.add_source(source_type='manual', title='Fixture intent', content='Only a technical color fixture; no business action or speech.')
        source_id = source['source_id']
        brief = topic_briefs.generate_brief(self.database, [source_id], generate_json=lambda *a, **kw: (full_flow_regression._brief_result(full_flow_regression._source_record(self.database, source_id)), {}))
        self.brief_id = brief['brief_id']
        topic_briefs.approve_brief(self.database, self.brief_id, topic_id='T-CREATIVE', angle_index=0)
        creative_learning.tag_angle(self.database, self.brief_id, angle_index=0, **TAGS_A)
        drafts = script_drafts.generate_drafts(self.database, self.brief_id, generate_json=lambda *a, **kw: (full_flow_regression._script_result(source_id), {}))
        self.draft_id = drafts['drafts'][0]['draft_id']

    def test_actual_approval_freezes_controlled_tags_and_source_identity(self):
        approved = script_drafts.approve_draft(self.database, self.draft_id)
        meta = approved['storyboard']['script_metadata']
        self.assertEqual(meta.get('creative_tags'), TAGS_A)
        self.assertEqual(meta.get('brief_id'), self.brief_id)
        self.assertEqual(meta.get('selected_angle_index'), 0)
        self.assertEqual(meta.get('creative_tag_snapshot', {}).get('authority'), 'approved_angle_intent_v1')
        snapshot_hash = meta['creative_tag_snapshot']['sha256']
        self.assertEqual(snapshot_hash, hashlib.sha256(json.dumps(TAGS_A, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        self.assertEqual(meta['approved_draft_id'], self.draft_id)
        self.assertEqual(len(meta['approved_script_sha256']), 64)
        first = production_contract.compile_production_spec(self.database, approved['job_id'], self.root, target_version=2)
        self.assertIsNotNone(first.spec)
        creative_learning.tag_angle(self.database, self.brief_id, angle_index=0, **TAGS_B)
        self.database.execute("UPDATE script_drafts SET script_json='{}' WHERE draft_id=?", (self.draft_id,))
        self.database.commit()
        repeated = script_drafts.approve_draft(self.database, self.draft_id)
        second = production_contract.compile_production_spec(self.database, approved['job_id'], self.root, target_version=2)
        self.assertEqual(repeated['storyboard'], approved['storyboard'])
        self.assertEqual(first.spec.spec_hash, second.spec.spec_hash)
        self.assertEqual(second.spec.as_dict()['runtime_storyboard']['script_metadata']['creative_tags'], TAGS_A)

    def test_unknown_tags_are_not_invented_from_angle_json(self):
        self.database.execute('DELETE FROM creative_angle_tags')
        self.database.commit()
        approved = script_drafts.approve_draft(self.database, self.draft_id)
        meta = approved['storyboard']['script_metadata']
        self.assertEqual(meta.get('creative_tags'), {})
        self.assertEqual(meta.get('creative_tag_snapshot', {}).get('provenance'), 'unknown')

    def test_replay_preflight_does_not_probe_real_provider_hosts(self):
        service = ManagementService(self.database, self.root, worker_mode='external', transport_preflight_enabled=True)
        with patch.dict('os.environ', {'FACTORY_PROVIDER_MODE':'replay'}), patch('factory.transport_diagnostics.run_preflight', return_value={'ready':False}) as probe:
            report = service.run_transport_preflight()
        self.assertTrue(report['ready'], 'Offline replay must reach its deterministic fixture boundary')
        self.assertEqual(report['mode'], 'replay')
        probe.assert_not_called()

    def test_live_preflight_still_uses_original_transport_gate(self):
        service = ManagementService(self.database, self.root, worker_mode='external', transport_preflight_enabled=True)
        for ready in [False, True]:
            with self.subTest(ready=ready), patch.dict('os.environ', {'FACTORY_PROVIDER_MODE':'live'}), patch('factory.transport_diagnostics.run_preflight', return_value={'ready':ready}) as probe:
                report = service.run_transport_preflight()
            self.assertEqual(report['ready'], ready)
            probe.assert_called_once()


if __name__ == '__main__':
    unittest.main()
