"""Content impact uses actual changed fields, never invented production progress."""
import json
from pathlib import Path
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/'scripts/management_ui/content-workspace.js'
class ContentWorkspaceTests(unittest.TestCase):
    def impact(self,before,after):
        self.assertTrue(MODULE.is_file(),'content change impact workspace missing')
        script="globalThis.window=globalThis;require(process.argv[1]);console.log(JSON.stringify(AVFContentWorkspace.impact(JSON.parse(process.argv[2]),JSON.parse(process.argv[3]))))"
        result=subprocess.run(['node','-e',script,str(MODULE),json.dumps(before),json.dumps(after)],capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)
    def test_unchanged_and_metadata_do_not_invent_affected_masters(self):
        original={'title':'A','beats':[{'speaker':'narration','dialogue':'说明','duration_sec':3}]}
        self.assertEqual(self.impact(original,original)['affected_beats'],[])
        result=self.impact(original,{**original,'title':'B'})
        self.assertEqual(result['affected_beats'],[])
        self.assertTrue(result['metadata_changed'])
    def test_multispeaker_uses_existing_ids_and_ignores_narration(self):
        original={'beats':[{'speaker':'a,b,c','duration_sec':5,'dialogue':'同框'}]}
        result=self.impact(original,{'beats':[{'speaker':'a,b,c','duration_sec':6,'dialogue':'三人说话'}]})
        self.assertEqual(result['affected_beats'],[1])
        self.assertEqual(result['character_ids'],['a','b','c'])
        self.assertEqual(result['before_seconds'],5)
        self.assertEqual(result['after_seconds'],6)
        self.assertTrue(result['needs_preflight'])
    def test_no_character_uses_no_fake_presenter(self):
        result=self.impact({'beats':[{'speaker':'narration','dialogue':'旧'}]},{'beats':[{'speaker':'narration','dialogue':'新'}]})
        self.assertEqual(result['character_ids'],[])
    def test_changed_visual_and_scene_context_are_tracked(self):
        result=self.impact({'beats':[{'visual_intent':'仓库','scene':'A'}]},{'beats':[{'visual_intent':'厂区','scene':'B'}]})
        self.assertEqual(result['affected_beats'],[1])
        self.assertIn('visual_intent',result['changes'][0]['fields'])
        self.assertIn('scene',result['changes'][0]['fields'])
    def test_removed_beat_tracks_previous_characters(self):
        result=self.impact({'beats':[{'speaker':'sales','duration_sec':4}]},{'beats':[]})
        self.assertEqual(result['character_ids'],['sales'])
        self.assertEqual(result['affected_beats'],[1])
    def test_offscreen_narrator_does_not_hide_three_visible_characters(self):
        old={'beats':[{'speaker':'narrator','characters':['buyer','liaison','admin'],'dialogue':'旧'}]}
        new={'beats':[{'speaker':'narrator','characters':['buyer','liaison','admin'],'dialogue':'新'}]}
        self.assertEqual(self.impact(old,new)['character_ids'],['buyer','liaison','admin'])
    def test_legacy_cast_alias_is_visible_even_without_speech(self):
        old={'beats':[{'speaker':'narration','cast':['buyer','liaison'],'visual_intent':'旧'}]}
        new={'beats':[{'speaker':'narration','cast':['buyer','liaison'],'visual_intent':'新'}]}
        self.assertEqual(self.impact(old,new)['character_ids'],['buyer','liaison'])

if __name__=='__main__':unittest.main()
