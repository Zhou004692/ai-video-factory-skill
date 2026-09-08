from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from factory import script_drafts

def sample():
    return {'title':'identity','creative_thesis':'stable person',
        'characters':[{'id':'sales1','name':'sales','gender':'male'},
                      {'id':'sales2','name':'sales','gender':'female'}],
        'beats':[{'beat':'hook','speaker':'sales1','dialogue':'hello',
                  'duration_sec':4,'visual_intent':'sales speaking','source_ids':[], 'broll_hint':''}]}

class CharacterIdentityContractTests(unittest.TestCase):
    def test_same_name_different_ids_requires_explicit_resolution(self):
        with self.assertRaisesRegex(ValueError,'character_identity_ambiguous'):
            script_drafts._normalize_variant(sample(),allowed_source_ids=set())

    def test_cached_ambiguous_script_cannot_enter_storyboard(self):
        with self.assertRaisesRegex(ValueError,'character_identity_ambiguous'):
            script_drafts._storyboard(sample())

    def test_distinct_people_with_distinct_names_remain_distinct(self):
        raw=sample();raw['characters'][1]['name']='event sales'
        result=script_drafts._normalize_variant(raw,allowed_source_ids=set())
        self.assertEqual(len(result['characters']),2)

    def test_one_person_in_different_scenes_keeps_one_identity(self):
        raw=sample();raw['characters']=raw['characters'][:1]
        raw['beats'].append({**deepcopy(raw['beats'][0]),'scene':'another office'})
        board=script_drafts._storyboard(raw)
        self.assertEqual([x['id'] for x in board['characters']],['sales1'])
