import importlib.util
from pathlib import Path
import unittest
try:
 import numpy as np
except ImportError:
 np=None

def load():
 p=Path(__file__).resolve().parents[1]/'scripts/recreation/priority_tracking.py'
 if not p.exists():raise AssertionError('Priority tracking implementation missing')
 s=importlib.util.spec_from_file_location('priority_tracking',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

@unittest.skipUnless(np is not None and importlib.util.find_spec('scipy') is not None,'optional NumPy/SciPy stage')
class PriorityTrackingTests(unittest.TestCase):
 def case(self):
  T=12;bend=.7+.28*np.sin(np.linspace(0,np.pi,T))
  q=np.zeros((T,3,3));q[:,0]=[0,1.5,-5.];q[:,1]=q[:,0]+[0,-.7,0]
  q[:,2]=q[:,1]+np.c_[np.zeros(T),-.7*np.cos(bend),.7*np.sin(bend)]
  prior=q.copy();prior[:,2]=prior[:,1]+[0,-.7*np.cos(.7),.7*np.sin(.7)]
  R=np.repeat(np.diag([1.,-1.,-1.])[None],T,axis=0);t=np.repeat([[0.,1.,0.]],T,axis=0)
  K=np.repeat([[[850.,0,540.],[0,850.,305.],[0,0,1.]]],T,axis=0)
  cams=dict(rotation_world_to_cv=R,translation_world_to_cv=t,intrinsics_px=K)
  uv=[]
  for n in range(T):
   v=R[n]@q[n,2]+t[n];h=K[n]@v;uv.append(h[:2]/h[2])
  options=dict(freeze=np.array([True,True]+[False]*8+[True,True]),foot_surface_offsets_world=np.repeat([[[0.,-.03,0.]]],T,axis=0),
   source_contact=np.zeros(T,bool),floor_normal=[0,1,0],floor_constant=0.,body_scale=1.,ankle_tolerance_px=3.,maxiter=100)
  # Frozen endpoints must be compatible with the tracking requirement.
  mask=~options['freeze'];return prior,np.array(uv),mask,cams,options

 def test_visible_motion_is_a_constraint_even_when_smoothing_prefers_flat_motion(self):
  q,uv,mask,cameras,kw=self.case();snapshot=q.copy()
  out=load().protect_limb_window(q,uv,mask,cameras,**kw)
  self.assertTrue(out['diagnostics']['candidate_usable'],out['diagnostics'])
  self.assertLessEqual(out['diagnostics']['protected_ankle_max_error_px'],3.00001)
  self.assertLess(out['diagnostics']['bone_length_max_error_world'],1e-10)
  np.testing.assert_array_equal(q,snapshot);np.testing.assert_array_equal(out['xyz'][kw['freeze']],q[kw['freeze']])
  np.testing.assert_array_equal(out['xyz'][:,0],q[:,0])

 def test_unreachable_target_is_not_accepted_and_does_not_relax_pixel_budget(self):
  q,uv,mask,cameras,kw=self.case();uv[mask,0]+=5000;kw['maxiter']=20
  out=load().protect_limb_window(q,uv,mask,cameras,**kw)
  self.assertFalse(out['diagnostics']['candidate_usable'])
  self.assertGreater(out['diagnostics']['protected_ankle_max_error_px'],3.)
  self.assertEqual(out['diagnostics']['ankle_tolerance_px'],3.)
  self.assertFalse(out['diagnostics']['source_3d_truth_recovered'])
  self.assertEqual(out['diagnostics']['solver_calls'],0)
  self.assertTrue(out['diagnostics']['fixed_base_reach_conflict_frames'])

 def test_frozen_frame_conflict_is_reported_instead_of_silently_ignored(self):
  q,uv,mask,cameras,kw=self.case();mask[0]=True;uv[0,0]+=100
  out=load().protect_limb_window(q,uv,mask,cameras,**kw)
  self.assertFalse(out['diagnostics']['candidate_usable']);self.assertEqual(out['diagnostics']['solver_calls'],0)
  np.testing.assert_array_equal(out['xyz'],q)

 def test_invalid_camera_or_moving_bone_lengths_fail_closed(self):
  q,uv,mask,cameras,kw=self.case()
  bad={k:v.copy() for k,v in cameras.items()};bad['rotation_world_to_cv'][0,0]*=-1
  with self.assertRaises(ValueError):load().protect_limb_window(q,uv,mask,bad,**kw)
  q[5,1,0]+=.1
  with self.assertRaises(ValueError):load().protect_limb_window(q,uv,mask,cameras,**kw)

 def test_incompatible_motion_and_angle_limits_are_rejected_without_relaxation(self):
  q,uv,mask,cameras,kw=self.case();kw['maxiter']=20
  out=load().protect_limb_window(q,uv,mask,cameras,adjacent_angle_limits_deg=np.zeros((11,2)),**kw)
  self.assertFalse(out['diagnostics']['candidate_usable'])
  self.assertFalse(out['diagnostics']['tracking_constraint_pass'] and out['diagnostics']['adjacent_angle_constraint_pass'])

class RaySphereGeometryTests(unittest.TestCase):
 def test_reach_cone_bound_is_conservative_and_handles_behind_camera(self):
  import math
  m=load()
  out=m.chain_reach_bound([0,0,0],[0,0,1],[1,0,5],.8,0.)
  self.assertAlmostEqual(out['distance_to_enclosing_cone'],1.)
  self.assertTrue(out['fixed_base_reach_conflict'])
  self.assertAlmostEqual(out['minimum_base_displacement_lower_bound'],.2)
  wide=m.chain_reach_bound([0,0,0],[0,0,1],[1,0,5],.8,.1)
  self.assertLess(wide['distance_to_enclosing_cone'],1.)
  inside=m.chain_reach_bound([0,0,0],[0,0,1],[0,0,5],.8,.1)
  self.assertEqual(inside['distance_to_enclosing_cone'],0.)
  behind=m.chain_reach_bound([0,0,0],[0,0,1],[0,0,-5],.8,0.)
  self.assertAlmostEqual(behind['distance_to_enclosing_cone'],5.)
  with self.assertRaises(ValueError):m.chain_reach_bound([0,0,0],[0,0,0],[1,0,5],.8,.1)
 def test_two_positive_intersections_do_not_depend_on_ray_direction_scale(self):
  out=load().ray_sphere_intersections([0,0,0],[0,0,5],[0,0,5],1.)
  self.assertEqual(out['points'],[[0.,0.,4.],[0.,0.,6.]])

 def test_tangent_miss_and_behind_camera_are_distinguished(self):
  m=load();tangent=m.ray_sphere_intersections([0,0,0],[0,0,1],[1,0,5],1.)
  self.assertEqual(tangent['points'],[[0.,0.,5.]])
  miss=m.ray_sphere_intersections([0,0,0],[0,0,1],[2,0,5],1.)
  self.assertEqual(miss['points'],[]);self.assertAlmostEqual(miss['line_closest_distance'],2.)
  behind=m.ray_sphere_intersections([0,0,0],[0,0,1],[0,0,-5],1.)
  self.assertEqual(behind['points'],[]);self.assertTrue(behind['line_intersects_sphere'])

 def test_invalid_ray_is_rejected(self):
  with self.assertRaises(ValueError):load().ray_sphere_intersections([0,0,0],[0,0,0],[0,0,5],1.)

 def test_parent_depth_upper_bound_has_known_collinear_and_tangent_controls(self):
  import math
  m=load();self.assertAlmostEqual(m.max_parent_ray_depth(0.,math.pi/6,1.,1.),3.)
  self.assertAlmostEqual(m.max_parent_ray_depth(math.pi/6,.001,1.,1.),2.)
  self.assertTrue(math.isinf(m.max_parent_ray_depth(0.,0.,1.,1.)))

 def test_pixel_cone_relaxation_can_only_increase_the_depth_upper_bound(self):
  m=load();exact=m.max_parent_ray_depth(.08,.12,.43,.375)
  optimistic=m.max_parent_ray_depth(.07,.10,.43,.375)
  self.assertGreaterEqual(optimistic,exact)

if __name__=='__main__':unittest.main()
