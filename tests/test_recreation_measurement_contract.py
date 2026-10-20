"""A lifting proxy is not an independent observation of a 3D virtual joint."""
import importlib.util,copy,unittest
from pathlib import Path

def load():
 spec=importlib.util.spec_from_file_location('pose_schema',Path(__file__).resolve().parents[1]/'scripts/recreation/pose_schema.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

class MeasurementContractTests(unittest.TestCase):
 def test_constructs_2d_proxies_from_projected_observed_parts_without_moving_rig(self):
  q=[[float(n),float(n*2)] for n in range(17)];q[0]=[100.,200.];q[7]=[300.,400.];q[8]=[500.,600.];before=copy.deepcopy(q)
  out=load().source_body_measurements(q)
  self.assertEqual(out['xy_h36m17'][0],[(q[1][0]+q[4][0])/2,(q[1][1]+q[4][1])/2])
  self.assertEqual(out['xy_h36m17'][8],[(q[11][0]+q[14][0])/2,(q[11][1]+q[14][1])/2])
  self.assertEqual(out['xy_h36m17'][7],[(out['xy_h36m17'][0][j]+out['xy_h36m17'][8][j])/2 for j in range(2)])
  self.assertEqual(q,before);self.assertEqual(out['virtual_joint_image_xy'],before)

 def test_perspective_midpoint_cannot_be_substituted_for_2d_observation_midpoint(self):
  # Two hips at unequal depth; the projected 3D midpoint is x=540,
  # while the mean of the observed projected hips is x=536.45833...
  q=[[540.,305.] for _ in range(17)];q[1]=[850*(-.1)/4+540,305.];q[4]=[850*.1/6+540,305.]
  out=load().source_body_measurements(q)
  self.assertAlmostEqual(out['xy_h36m17'][0][0],536.4583333333333)
  self.assertGreater(abs(out['xy_h36m17'][0][0]-q[0][0]),3.)

 def test_derived_proxies_do_not_multiply_independent_constraints_or_invent_head_binding(self):
  out=load().source_body_measurements([[0.,0.] for _ in range(17)])
  self.assertEqual(out['independent_fit_indices'],[1,2,3,4,5,6,11,12,13,14,15,16])
  self.assertEqual(out['derived_auxiliary_indices'],[0,7,8]);self.assertEqual(out['unbound_indices'],[9,10])
  self.assertIsNone(out['xy_h36m17'][9]);self.assertIsNone(out['xy_h36m17'][10]);self.assertFalse(out['measurement_truth_verified'])

 def test_bad_projection_fails_closed(self):
  for q in [[[0.,0.]]*16,[[0.,0.,0.]]*17,[[float('nan'),0.]]*17]:
   with self.assertRaises(ValueError):load().source_body_measurements(q)

if __name__=='__main__':unittest.main()
