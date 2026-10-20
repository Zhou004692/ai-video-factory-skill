import importlib.util
from pathlib import Path
import unittest
try:
 import numpy as np
except ImportError:
 np=None
import importlib.util as _util

def load():
 p=Path(__file__).resolve().parents[1]/'scripts/recreation/kinematic_fit.py'
 s=importlib.util.spec_from_file_location('recreation_kinematic_fit',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

@unittest.skipUnless(np is not None and _util.find_spec('scipy') is not None,'optional fit stage requires NumPy/SciPy')
class KinematicFitTests(unittest.TestCase):
 def inputs(self):
  pose=np.array([[0,1.5,95],[.3,.8,95],[0,0,95]],float)
  truth=np.repeat(pose[None],12,axis=0);prior=truth.copy()
  prior[2:10,:,1]+=.25*np.sin(np.linspace(0,np.pi,8))[:,None]
  uv=np.stack([540+100*truth[:,:,0]/(100-truth[:,:,2]),305-100*truth[:,:,1]/(100-truth[:,:,2])],axis=-1)
  return truth,prior,uv

 def test_fixed_material_contact_targets_remove_tangential_slide_without_changing_bone_lengths(self):
  truth,_,uv=self.inputs();prior=truth.copy();prior[2:10,:,0]+=.2*np.sin(np.linspace(0,np.pi,8))[:,None]
  freeze=np.zeros(12,bool);freeze[:2]=True;freeze[-2:]=True
  target=truth[:,[2]].copy()
  r=load().fit_window(prior,[-1,0,1],uv,np.zeros((12,3)),mutable_bones=[1,2],body_scale=1.,focal_px=100.,
   floor_normal=[0,1,0],floor_constant=0.,foot_joints=[2],sole_offsets=np.zeros((12,1,3)),contact_weights=np.ones((12,1)),freeze=freeze,
   contact_targets_world=target,contact_position_weights=np.ones((12,1)),contact_position_sigma_units=.002,max_nfev=100)
  self.assertLess(abs(r['xyz'][:,2,0]).max(),.01)
  np.testing.assert_array_equal(r['xyz'][freeze],prior[freeze])

 def test_missing_contact_position_weights_do_not_silently_lock_to_supplied_targets(self):
  truth,_,uv=self.inputs();target=truth[:,[2]].copy();target[:,:,0]+=10
  with self.assertRaises(ValueError):
   load().fit_window(truth,[-1,0,1],uv,np.ones((12,3)),mutable_bones=[1,2],body_scale=1.,focal_px=100.,floor_normal=[0,1,0],floor_constant=0.,
    foot_joints=[2],sole_offsets=np.zeros((12,1,3)),contact_weights=np.zeros((12,1)),freeze=np.zeros(12,bool),contact_targets_world=target)

 def test_contact_material_offset_is_separate_from_changing_height_minimum(self):
  truth,_,uv=self.inputs();offset=np.zeros((12,1,3));offset[:,:,0]=.25
  target=truth[:,[2]]+offset
  r=load().fit_window(truth,[-1,0,1],uv,np.ones((12,3)),mutable_bones=[1,2],body_scale=1.,focal_px=100.,floor_normal=[0,1,0],floor_constant=0.,
   foot_joints=[2],sole_offsets=np.zeros((12,1,3)),contact_weights=np.ones((12,1)),freeze=np.zeros(12,bool),
   contact_targets_world=target,contact_position_offsets=offset,contact_position_weights=np.ones((12,1)))
  np.testing.assert_allclose(r['xyz'],truth,atol=1e-9)
  self.assertEqual(r['diagnostics']['contact_position_active_samples'],12)

 def test_joint_projection_contact_and_fixed_bones_without_retiming_or_moving_frozen_context(self):
  truth,prior,uv=self.inputs();snapshot=prior.copy();freeze=np.zeros(12,bool);freeze[:2]=True;freeze[-2:]=True
  out=load().fit_window(prior,[-1,0,1],uv,np.ones((12,3)),mutable_bones=[1,2],body_scale=1.,focal_px=100.,
   floor_normal=[0,1,0],floor_constant=0.,foot_joints=[2],sole_offsets=np.zeros((12,1,3)),contact_weights=np.ones((12,1)),freeze=freeze,
   projection_sigma_px=1.,contact_sigma_units=.003,prior_sigma_units=.5,correction_acceleration_sigma_units=.1,max_nfev=100)
  q=out['xyz'];self.assertLess(abs(q[:,2,1]).max(),.015)
  for j,p in [(1,0),(2,1)]:np.testing.assert_allclose(np.linalg.norm(q[:,j]-q[:,p],axis=-1),np.linalg.norm(prior[:,j]-prior[:,p],axis=-1),atol=1e-10)
  np.testing.assert_array_equal(q[freeze],prior[freeze]);np.testing.assert_array_equal(prior,snapshot)
  self.assertLess(out['diagnostics']['projection_rmse_after_px'],out['diagnostics']['projection_rmse_before_px'])
  self.assertFalse(out['diagnostics']['source_3d_truth_recovered'])

 def test_unknown_support_does_not_invent_contact_or_force_lowest_foot_to_floor(self):
  truth,_,_=self.inputs();prior=truth.copy();prior[:,:,1]+=.25
  uv=np.stack([540+100*prior[:,:,0]/(100-prior[:,:,2]),305-100*prior[:,:,1]/(100-prior[:,:,2])],axis=-1)
  out=load().fit_window(prior,[-1,0,1],uv,np.ones((12,3)),mutable_bones=[1,2],body_scale=1.,focal_px=100.,floor_normal=[0,1,0],floor_constant=0.,
   foot_joints=[2],sole_offsets=np.zeros((12,1,3)),contact_weights=np.zeros((12,1)),freeze=np.zeros(12,bool))
  np.testing.assert_allclose(out['xyz'],prior,atol=1e-9)

 def test_local_limb_fit_cannot_move_the_frozen_root(self):
  _,prior,uv=self.inputs()
  r=load().fit_window(prior,[-1,0,1],uv,np.ones((12,3)),mutable_bones=[1,2],body_scale=1.,focal_px=100.,floor_normal=[0,1,0],floor_constant=0.,
   foot_joints=[],sole_offsets=np.zeros((12,0,3)),contact_weights=np.zeros((12,0)),freeze=np.zeros(12,bool),fit_root=False)
  np.testing.assert_array_equal(r['xyz'][:,0],prior[:,0])

 def test_inertial_mode_does_not_preserve_prior_jitter_in_unobserved_frames(self):
  truth,_,_=self.inputs();prior=truth.copy();prior[3:9,:,0]+=np.array([.2,-.2,.2,-.2,.2,-.2])[:,None]
  uv=np.stack([540+100*truth[:,:,0]/(100-truth[:,:,2]),305-100*truth[:,:,1]/(100-truth[:,:,2])],axis=-1)
  weights=np.ones((12,3));weights[3:9]=0;freeze=np.ones(12,bool);freeze[3:9]=False
  r=load().fit_window(prior,[-1,0,1],uv,weights,mutable_bones=[1,2],body_scale=1.,focal_px=100.,floor_normal=[0,1,0],floor_constant=0.,
   foot_joints=[],sole_offsets=np.zeros((12,0,3)),contact_weights=np.zeros((12,0)),freeze=freeze,temporal_mode='motion')
  self.assertLess(abs(np.diff(r['xyz'][:,0,0],n=2)).mean(),abs(np.diff(prior[:,0,0],n=2)).mean()*.3)

class SupportAdmissionTests(unittest.TestCase):
 def test_floor_geometry_alone_cannot_accept_unknown_source_states(self):
  r=load().audit_support([[0.,.4]],['unknown'])
  self.assertFalse(r['accepted'])
  self.assertTrue(r['geometry_passed']);self.assertEqual(r['unknown_state_frames'],[0])

 def test_floating_is_checked_even_without_a_contact_candidate(self):
  r=load().audit_support([[.2,.3],[.1,.4],[.01,.4],[.2,.3],[-.1,.2]],['grounded','unknown','grounded','flight','grounded'],gap_tolerance=.03)
  self.assertEqual(r['grounded_float_frames'],[0]);self.assertEqual(r['unresolved_float_frames'],[1])
  self.assertEqual(r['penetration_frames'],[4]);self.assertEqual(r['source_flight_frames'],[3]);self.assertFalse(r['accepted'])

if __name__=='__main__':unittest.main()
