"""Regressions for lost/reacquired guides and conflicting temporal estimates."""
import importlib.util
import math
from pathlib import Path
import unittest

def load():
    p=Path(__file__).resolve().parents[1]/'scripts/recreation/temporal.py'
    spec=importlib.util.spec_from_file_location('recreation_temporal',p)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

class TemporalGuideTests(unittest.TestCase):
    def test_first_measurement_after_unknown_opening_respects_explicit_render_prior(self):
        g=load().AngleGuide(max_speed=math.pi*4,initial_angle=0.)
        for n in range(8):self.assertEqual(g.update(n/30,None)['angle'],0.)
        r=g.update(8/30,math.radians(119))
        self.assertLessEqual(abs(r['angle']),math.pi*4/30+1e-9)
        self.assertEqual(r['state'],'reacquiring')

    def test_unknown_is_not_an_observed_zero_and_time_is_strict(self):
        g=load().AngleGuide(max_speed=math.pi*4)
        self.assertIsNone(g.update(0,None)['angle'])
        self.assertEqual(g.update(1/30,1.)['state'],'observed')
        with self.assertRaises(ValueError):g.update(1/30,1.)

    def test_reacquisition_cannot_snap_119_degrees_after_a_gap(self):
        g=load().AngleGuide(max_speed=math.pi*4)
        g.update(0,0.)
        for n in range(1,8):
            self.assertEqual(g.update(n/30,None)['state'],'held_unknown')
        r=g.update(8/30,math.radians(119))
        self.assertEqual(r['state'],'reacquiring')
        self.assertLessEqual(abs(r['angle']),math.pi*4/30+1e-9)
        self.assertFalse(r['source_verified'])

    def test_wrap_and_reliable_fast_motion_are_not_frozen(self):
        g=load().AngleGuide(max_speed=math.pi*4)
        g.update(0,3.12);r=g.update(1/30,-3.12)
        self.assertLess(abs(r['angle']-3.12),.05)
        for n in range(2,15):r=g.update(n/30,-3.12+(n-1)*.12)
        self.assertGreater(r['angle'],4.)

    def test_conflict_gate_rejects_opposed_estimates_without_inventing_mean(self):
        f=load().direction_consensus
        r=f([[1,0,0],[0,0,1]],[.4,.6],threshold_degrees=30,min_weight=.2)
        self.assertIsNone(r['direction']);self.assertEqual(r['state'],'conflict')
        self.assertAlmostEqual(r['max_disagreement_degrees'],90)
        r=f([[1,0,0],[.99,.01,0]],[.4,.6],threshold_degrees=30,min_weight=.2)
        self.assertEqual(r['state'],'consensus');self.assertAlmostEqual(math.dist(r['direction'],[0,0,0]),1)
        # A weak window boundary is logged, but does not veto the strong context.
        r=f([[1,0,0],[0,0,1]],[.01,.99],threshold_degrees=30,min_weight=.2)
        self.assertEqual(r['state'],'consensus');self.assertEqual(r['active_candidates'],[1])

    def test_confidence_is_not_enough_when_vector_is_below_heatmap_resolution(self):
        f=load().resolved_axis
        self.assertIsNone(f([4,0],.99,grid_px=8,minimum_score=.35))
        self.assertAlmostEqual(f([20,0],.8,grid_px=8,minimum_score=.35),0)
        self.assertIsNone(f([20,0],.2,grid_px=8,minimum_score=.35))

    def test_dense_face_needs_resolution_and_drives_mouth_when_resolved(self):
        f=load().face_controls
        p=[[0.,0.] for _ in range(68)]
        p[36]=[-40,0];p[39]=[-20,0];p[42]=[20,0];p[45]=[40,0]
        for a,b,x in [(37,41,-35),(38,40,-25),(43,47,25),(44,46,35)]:p[a]=[x,-4];p[b]=[x,4]
        p[60]=[-20,30];p[64]=[20,30];p[62]=[0,25];p[66]=[0,35]
        r=f(p,[.9]*68,grid_px=2)
        self.assertEqual(r['state'],'resolved');self.assertGreater(r['face']['mouth'],.1)
        p[66]=[0,25];self.assertLess(f(p,[.9]*68,grid_px=2)['face']['mouth'],r['face']['mouth'])
        small=[[x*.1,y*.1] for x,y in p]
        self.assertEqual(f(small,[.99]*68,grid_px=2)['state'],'unresolved')
        self.assertIsNone(f(small,[.99]*68,grid_px=2)['face'])

    def test_palm_is_a_resolved_2d_guide_not_a_3d_measurement(self):
        p=[[0.,0.] for _ in range(21)]
        for j in [5,9,13,17]:p[j]=[20.,0.]
        r=load().palm_axis(p,[.9]*21,grid_px=4)
        self.assertEqual(r['angle'],0.);self.assertFalse(r['depth_observed'])
        self.assertIsNone(load().palm_axis(p,[.1]*21,grid_px=4)['angle'])

if __name__=='__main__':unittest.main()
