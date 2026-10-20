import importlib.util
from pathlib import Path
import unittest

def load():
 p=Path(__file__).resolve().parents[1]/'scripts/recreation/retarget.py';s=importlib.util.spec_from_file_location('retarget',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

class HeadProjectionTests(unittest.TestCase):
 def test_source_projection_stays_fixed_when_depth_is_adapted_to_the_body(self):
  point=[10.,20.,30.];copy=point[:];f=100.
  q=load().preserve_pinhole_projection(point,f,45.)
  self.assertEqual(q[2],45.);self.assertEqual(point,copy)
  for k in [0,1]:self.assertAlmostEqual(point[k]/(f-point[2]),q[k]/(f-q[2]),places=12)
 def test_points_behind_camera_or_nonfinite_depth_are_rejected(self):
  f=load().preserve_pinhole_projection
  for p,z in [([0,0,100],20),([0,0,20],100),([0,float('nan'),20],30)]:
   with self.assertRaises(ValueError):f(p,100,z)

if __name__=='__main__':unittest.main()
