"""Repeated pictures must not acquire mismatched camera/actor animation time."""
from pathlib import Path
import importlib.util,unittest

def load():
 p=Path(__file__).resolve().parents[1]/'scripts/recreation/temporal.py';spec=importlib.util.spec_from_file_location('time_contract',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

class ContentTimeTests(unittest.TestCase):
 def evidence(self,a,b,same=True):return dict(source_frames=[a,b],same_content=same,evidence_id=f'review-F{a}-F{b}')
 def test_reviewed_hold_groups_keep_every_frame_and_actual_pts(self):
  out=load().content_time_map([10,11,12,13],[0.,.033,.067,.1],[self.evidence(11,12)])
  self.assertEqual(out['representative_source_frames'],[10,11,11,13]);self.assertEqual(out['source_timestamps_seconds'],[0.,.033,.067,.1])
  self.assertEqual(out['source_frames'],[10,11,12,13]);self.assertFalse(out['source_capture_time_recovered'])

 def test_pinned_calibration_anchor_is_retained_without_retiring_heldout(self):
  out=load().content_time_map([10,11,12],[0.,.033,.067],[self.evidence(10,11)],pinned_source_frames=[11])
  self.assertEqual(out['representative_source_frames'],[11,11,12])
  with self.assertRaisesRegex(ValueError,'pinned'):load().content_time_map([10,11],[0.,.033],[self.evidence(10,11)],pinned_source_frames=[10,11])

 def test_unreviewed_low_difference_or_false_evidence_does_not_freeze(self):
  f=load().content_time_map
  self.assertEqual(f([0,1],[0.,.033],[self.evidence(0,1,False)])['representative_source_frames'],[0,1])
  with self.assertRaises(ValueError):f([0,1],[0.,.033],[dict(source_frames=[0,1],same_content=True)])
  with self.assertRaises(ValueError):f([0,1],[0.,.033],[dict(source_frames=[0,1],same_content=1,evidence_id='not-bool')])

 def test_rejects_nonadjacent_pairs_reordered_frames_duplicate_evidence_and_bad_pts(self):
  f=load().content_time_map
  with self.assertRaises(ValueError):f([0,1,2],[0.,.033,.067],[self.evidence(0,2)])
  with self.assertRaises(ValueError):f([1,0],[0.,.033],[])
  with self.assertRaises(ValueError):f([0,1],[0.,0.],[])
  with self.assertRaises(ValueError):f([0,1],[0.,float('nan')],[])
  with self.assertRaises(ValueError):f([0,1],[0.,.033],[self.evidence(0,1),self.evidence(0,1)])

 def test_groups_are_explicit_and_inputs_are_unchanged(self):
  ids=[0,1,2,3];t=[0.,.033,.067,.1];pairs=[self.evidence(0,1),self.evidence(1,2)]
  out=load().content_time_map(ids,t,pairs)
  self.assertEqual(out['representative_source_frames'],[0,0,0,3]);self.assertEqual(ids,[0,1,2,3]);self.assertEqual(len(pairs),2)
  self.assertEqual(out['group_source_frames'],[[0,1,2],[3]])

if __name__=='__main__':unittest.main()
