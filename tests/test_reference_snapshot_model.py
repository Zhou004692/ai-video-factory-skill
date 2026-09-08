"""Frozen model capabilities must survive a legacy worker default."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import sys
from dotenv import load_dotenv
load_dotenv(sys.argv[1], override=True)
sys.path.insert(0, sys.argv[2])
from factory import character_assets, production_config
from factory.clients import seedance
from factory.steps import s3_shots
assert seedance.DEFAULT_MODEL == sys.argv[3]
model = sys.argv[4]
snapshot = production_config.freeze_generation_snapshot({'video_model': model, 'reference_video_budget': 4 if '2-5' in model else 3})
members = [{'id': x, 'name': x, 'generation_mode': 'ai_generated'} for x in ('a','b','c')]
anchors = {character_assets.character_identity_key(x): s3_shots.DialogueAnchor(video_url='https://example.invalid/'+x['id']+'.mp4',duration_sec=2) for x in members}
shot = production_config.attach_runtime_snapshot({'idx': 1, 'duration_sec': 8, 'cast': ['a','b','c'], 'speaker': 'a'}, snapshot)
request = s3_shots._dialogue_request_kwargs(shot,None,character=members[0],cast_characters=members[1:],identity_anchors=anchors,scene_video_anchor=s3_shots.DialogueAnchor(video_url='https://example.invalid/previous.mp4',duration_sec=2))
assert len(request['reference_videos']) == 4
assert len([r for r in request['reference_manifest'] if r['kind']=='video']) == 4
print('four_frozen_references_retained')
'''


class ReferenceSnapshotModelTests(unittest.TestCase):
    def probe(self, default_model, frozen_model):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td)/'.env'
            env_file.write_text(f'ARK_VIDEO_MODEL={default_model}\nSEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET=3\n', encoding='utf-8')
            return subprocess.run([sys.executable, '-c', PROBE, str(env_file), str(ROOT/'scripts'), default_model, frozen_model],
                text=True, capture_output=True, timeout=30, env={**os.environ, 'PYTHONIOENCODING':'utf-8'})

    def test_legacy_worker_default_does_not_cap_frozen_25_at_three(self):
        result = self.probe('doubao-seedance-2-0-260128', 'doubao-seedance-2-5-260628')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('four_frozen_references_retained', result.stdout)

    def test_new_default_cannot_expand_a_frozen_legacy_model(self):
        result = self.probe('doubao-seedance-2-5-260628', 'doubao-seedance-2-0-260128')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('budget of 3 reference videos', result.stderr)
