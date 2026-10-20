"""Regress the target-policy error, not the analytic IK mathematics."""
from pathlib import Path
import importlib.util
import unittest


def module():
    p=Path(__file__).resolve().parents[1]/'scripts/recreation/motion_control.py'
    spec=importlib.util.spec_from_file_location('motion_control_contact_test',p)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


class ContactTargetPlanTests(unittest.TestCase):
    def test_real_anticipatory_weights_are_blocked_before_stance_evidence(self):
        # Actual S1 contact-ramp weights at F93--95, before candidate F96.
        plan=[{'frame':n,'mode':'unknown','evidence_kind':'unknown','weight':w,
               'target':[-230.858933608798,-218.73251972548468,-264.29145224330887]}
              for n,w in [(93,.103515625),(94,.5),(95,.896484375)]]
        result=module().gate_contact_target_plan(plan)
        self.assertEqual([r['weight'] for r in result['rows']],[0.,0.,0.])
        self.assertEqual([r['requested_weight'] for r in result['rows']],[.103515625,.5,.896484375])
        self.assertEqual(result['blocked_frames'],[93,94,95])
        self.assertEqual(plan[1]['weight'],.5)  # preserve evidence

    def test_inferred_stationary_is_not_automatically_a_static_position_lock(self):
        plan=[{'frame':96,'mode':'stance','evidence_kind':'inferred','weight':1.,'target':[1,2,3]}]
        self.assertEqual(module().gate_contact_target_plan(plan)['rows'][0]['weight'],0)
        allowed=module().gate_contact_target_plan(plan,allow_inferred_lock=True)
        self.assertEqual(allowed['rows'][0]['weight'],1)
        self.assertFalse(allowed['source_truth_verified'])

    def test_pivot_moving_contact_and_unknown_stay_unlocked_with_inferred_opt_in(self):
        for mode in ['pivot','moving_contact','flight','swing','unknown']:
            plan=[{'frame':0,'mode':mode,'evidence_kind':'inferred','weight':.7,'target':[1,2,3]}]
            self.assertEqual(module().gate_contact_target_plan(plan,allow_inferred_lock=True)['rows'][0]['weight'],0)

    def test_source_confirmation_preserves_exact_target_and_requested_ramp(self):
        plan=[{'frame':2,'mode':'stance','evidence_kind':'source_confirmed','weight':.4,'target':[1,2,3]}]
        row=module().gate_contact_target_plan(plan)['rows'][0]
        self.assertEqual(row['target'],[1,2,3]);self.assertEqual(row['weight'],.4)

    def test_invalid_or_out_of_order_plan_is_not_silently_accepted(self):
        good={'frame':1,'mode':'stance','evidence_kind':'source_confirmed','weight':1.,'target':[1,2,3]}
        for patch in [{'weight':-1},{'weight':float('nan')},{'mode':'stationary'},{'evidence_kind':'guessed'},{'target':[0,float('inf'),1]}]:
            with self.assertRaises(ValueError):module().gate_contact_target_plan([dict(good,**patch)])
        with self.assertRaises(ValueError):module().gate_contact_target_plan([good,good])


if __name__=='__main__':unittest.main()
