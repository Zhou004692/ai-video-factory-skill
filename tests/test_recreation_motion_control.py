from pathlib import Path
import importlib.util
import math
import unittest


def load():
    p=Path(__file__).resolve().parents[1]/'scripts/recreation/motion_control.py'
    s=importlib.util.spec_from_file_location('recreation_motion_control',p)
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m


class BendGuideTests(unittest.TestCase):
    def setUp(self):
        self.m=load();self.assertTrue(hasattr(self.m,'BendGuide'),'Missing continuous CPU bend guide')

    def test_straight_start_stays_unknown_without_inventing_a_bend_axis(self):
        r=self.m.BendGuide().update(0,[0,0,0],[0,-1,0],[0,-2,0],confidence=1)
        self.assertIsNone(r['pole']);self.assertEqual(r['state'],'unknown')

    def test_near_straight_flip_is_held_then_reacquires_without_a_single_frame_snap(self):
        g=self.m.BendGuide(min_bend_ratio=.03,max_speed=math.pi)
        a=g.update(0,[0,0,0],[.3,-1,0],[0,-2,0],confidence=1)
        b=g.update(.1,[0,0,0],[-.001,-1,0],[0,-2,0],confidence=1)
        c=g.update(.2,[0,0,0],[-.3,-1,0],[0,-2,0],confidence=1)
        self.assertGreater(b['pole'][0],.999)
        self.assertLessEqual(math.acos(max(-1,min(1,sum(x*y for x,y in zip(b['pole'],c['pole']))))),math.pi*.1+1e-8)
        self.assertEqual(b['state'],'held_weak_bend');self.assertEqual(c['state'],'limited_reacquisition')
        self.assertFalse(a['source_truth_verified'])

    def test_transport_follows_chain_axis_with_unit_perpendicular_pole(self):
        g=self.m.BendGuide()
        g.update(0,[0,0,0],[.3,-1,0],[0,-2,0],confidence=1)
        r=g.update(.1,[0,0,0],[-1,-.001,0],[-2,0,0],confidence=0)
        self.assertAlmostEqual(r['pole'][0],0,places=8)
        self.assertAlmostEqual(sum(x*x for x in r['pole']),1,places=8)

    def test_translation_and_uniform_scale_do_not_change_pole(self):
        a=self.m.BendGuide().update(0,[0,0,0],[.3,-1,0],[0,-2,0],confidence=1)
        b=self.m.BendGuide().update(0,[10,20,30],[10.9,17,30],[10,14,30],confidence=1)
        for x,y in zip(a['pole'],b['pole']):self.assertAlmostEqual(x,y,places=8)

    def test_half_turn_and_invalid_clock_do_not_produce_nan(self):
        g=self.m.BendGuide();g.update(0,[0,0,0],[.3,-1,0],[0,-2,0],confidence=1)
        r=g.update(.1,[0,0,0],[0,1,0],[0,2,0],confidence=0)
        self.assertTrue(all(math.isfinite(x) for x in r['pole']))
        self.assertTrue(r['axis_half_turn_ambiguous'])
        with self.assertRaises(ValueError):g.update(.1,[0,0,0],[0,1,0],[0,2,0],confidence=0)


class ContactGuideTests(unittest.TestCase):
    def setUp(self):
        self.m=load();self.assertTrue(hasattr(self.m,'ContactGuide'),'Missing explicit contact-state CPU guide')

    def test_low_foot_height_alone_never_invents_support_or_authorizes_locking(self):
        g=self.m.ContactGuide()
        r=g.update(0,gap=0,speed=0,confidence=1,mode='unknown',evidence_kind='unknown')
        self.assertEqual(r['state'],'unknown');self.assertEqual(r['lock_weight'],0)

    def test_inferred_support_is_not_source_truth_and_cannot_silently_lock(self):
        g=self.m.ContactGuide(confirm_seconds=.1)
        g.update(0,gap=0,speed=0,confidence=1,mode='stance',evidence_kind='inferred')
        r=g.update(.11,gap=0,speed=0,confidence=1,mode='stance',evidence_kind='inferred')
        self.assertEqual(r['state'],'stance_candidate');self.assertEqual(r['lock_weight'],0)
        self.assertFalse(r['source_truth_verified'])

    def test_confirmation_uses_seconds_hysteresis_and_immediate_release_on_swing(self):
        g=self.m.ContactGuide(confirm_seconds=.1,enter_gap=.02,exit_gap=.05,enter_speed=.2,exit_speed=.4)
        self.assertEqual(g.update(0,gap=0,speed=0,confidence=1,mode='stance',evidence_kind='source_confirmed')['lock_weight'],0)
        r=g.update(.12,gap=.01,speed=.1,confidence=1,mode='stance',evidence_kind='source_confirmed')
        self.assertEqual(r['lock_weight'],1)
        r=g.update(.16,gap=.04,speed=.3,confidence=1,mode='stance',evidence_kind='source_confirmed')
        self.assertEqual(r['lock_weight'],1)
        r=g.update(.2,gap=.04,speed=.3,confidence=1,mode='swing',evidence_kind='source_confirmed')
        self.assertEqual(r['state'],'swing');self.assertEqual(r['lock_weight'],0)

    def test_pivot_moving_contact_and_unknown_never_become_static_foot_locks(self):
        for mode in ['pivot','moving_contact','flight','unknown']:
            g=self.m.ContactGuide(allow_inferred_lock=True)
            r=g.update(0,gap=0,speed=0,confidence=1,mode=mode,evidence_kind='inferred')
            self.assertEqual(r['lock_weight'],0)

    def test_material_point_velocity_separates_tangent_and_normal_motion(self):
        r=self.m.point_velocity([0,0,0],[.1,.2,0],.1,[0,1,0],body_scale=2)
        self.assertAlmostEqual(r['tangent_units_per_second'],.5)
        self.assertAlmostEqual(r['normal_units_per_second'],1)
        self.assertFalse(r['contact_verified'])


if __name__=='__main__':unittest.main()
