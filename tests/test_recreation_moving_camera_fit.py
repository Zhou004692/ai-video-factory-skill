"""Explicit-camera fitting controls, with independently computed image targets."""
import importlib.util
from pathlib import Path
import unittest
try:
 import numpy as np
except ImportError:
 np=None

def load():
 p=Path(__file__).resolve().parents[1]/'scripts/recreation/kinematic_fit.py'
 s=importlib.util.spec_from_file_location('moving_camera_fit',p)
 m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

@unittest.skipUnless(np is not None and importlib.util.find_spec('scipy') is not None,'optional NumPy/SciPy fitting')
class MovingCameraFitTests(unittest.TestCase):
 def case(self):
  truth=np.repeat(np.array([[[0.,1.5,-5.],[.3,.8,-5.],[0.,0.,-5.]]]),12,axis=0)
  angles=np.linspace(-.05,.05,12);rot=[];tv=[]
  for i,a in enumerate(angles):
   c,s=np.cos(a),np.sin(a)
   R=np.diag([1.,-1.,-1.])@np.array([[c,0,s],[0,1,0],[-s,0,c]])
   C=np.array([.3*np.sin(i/5),1.1,.1*np.cos(i/5)])
   rot.append(R);tv.append(-R@C)
  K=np.repeat(np.array([[[850.,0,530.],[0,910.,299.],[0,0,1.]]]),12,axis=0)
  cameras=dict(rotation_world_to_cv=np.array(rot),translation_world_to_cv=np.array(tv),intrinsics_px=K)
  uv=[]
  for n in range(12):
   v=(np.array(rot[n])@truth[n].T+np.array(tv[n])[:,None])
   h=K[n]@v;uv.append((h[:2]/h[2]).T)
  return truth,np.array(uv),cameras

 def run_fit(self,q,uv,**kw):
  options=dict(mutable_bones=[1,2],body_scale=1.,focal_px=100.,floor_normal=[0,1,0],floor_constant=0.,
   foot_joints=[2],sole_offsets=np.zeros((12,1,3)),contact_weights=np.ones((12,1)),freeze=np.zeros(12,bool))
  options.update(kw)
  return load().fit_window(q,[-1,0,1],uv,np.ones((12,3)),**options)

 def test_moving_camera_does_not_turn_camera_motion_into_body_motion(self):
  truth,uv,cameras=self.case();snapshot={k:v.copy() for k,v in cameras.items()}
  out=self.run_fit(truth,uv,camera_sequence=cameras)
  np.testing.assert_allclose(out['xyz'],truth,atol=1e-9)
  self.assertLess(out['diagnostics']['projection_rmse_after_px'],1e-8)
  self.assertEqual(out['diagnostics']['projection_camera_mode'],'explicit_world_to_cv')
  for k in cameras:np.testing.assert_array_equal(cameras[k],snapshot[k])

 def test_contact_and_frozen_context_use_world_coordinates_under_moving_camera(self):
  truth,uv,cameras=self.case();prior=truth.copy();prior[2:10,:,1]+=.2*np.sin(np.linspace(0,np.pi,8))[:,None]
  frozen=np.zeros(12,bool);frozen[:2]=True;frozen[-2:]=True
  out=self.run_fit(prior,uv,camera_sequence=cameras,freeze=frozen)
  self.assertLess(abs(out['xyz'][:,2,1]).max(),.015)
  self.assertLess(out['diagnostics']['projection_rmse_after_px'],out['diagnostics']['projection_rmse_before_px'])
  np.testing.assert_array_equal(out['xyz'][frozen],prior[frozen])
  self.assertLess(out['diagnostics']['bone_length_max_error_world'],1e-10)

 def test_unknown_contact_remains_airborne_without_static_position_lock(self):
  truth,_,cameras=self.case();truth[:,:,1]+=.25
  uv=[]
  for n in range(12):
   v=cameras['rotation_world_to_cv'][n]@truth[n].T+cameras['translation_world_to_cv'][n,:,None]
   h=cameras['intrinsics_px'][n]@v;uv.append((h[:2]/h[2]).T)
  out=self.run_fit(truth,np.array(uv),camera_sequence=cameras,contact_weights=np.zeros((12,1)))
  np.testing.assert_allclose(out['xyz'],truth,atol=1e-9)
  self.assertEqual(out['diagnostics']['contact_position_active_samples'],0)

 def test_explicit_legacy_camera_matches_legacy_default(self):
  q=np.repeat(np.array([[[0,1.5,95],[.3,.8,95],[0,0,95]]],float),12,axis=0)
  uv=np.stack([540+100*q[:,:,0]/(100-q[:,:,2]),305-100*q[:,:,1]/(100-q[:,:,2])],axis=-1)
  cameras=dict(rotation_world_to_cv=np.repeat(np.diag([1.,-1.,-1.])[None],12,axis=0),
   translation_world_to_cv=np.repeat([[0.,0.,100.]],12,axis=0),
   intrinsics_px=np.repeat([[[100.,0,540.],[0,100.,305.],[0,0,1.]]],12,axis=0))
  prior=q.copy();prior[2:10,:,1]+=.1*np.sin(np.linspace(0,np.pi,8))[:,None]
  a=self.run_fit(prior,uv);b=self.run_fit(prior,uv,camera_sequence=cameras)
  # Equivalent arithmetic orders perturb finite-difference optimizer paths;
  # require agreement to 1e-5 body units, not bit-identical iterations.
  np.testing.assert_allclose(a['xyz'],b['xyz'],atol=1e-5)

 def test_invalid_or_implicitly_broadcast_camera_arrays_fail_closed(self):
  q,uv,cameras=self.case()
  for kind in ['reflection','nonorthogonal','nan','single_K','negative_fx','missing_t']:
   bad={k:v.copy() for k,v in cameras.items()}
   if kind=='reflection':bad['rotation_world_to_cv'][0,0]*=-1
   elif kind=='nonorthogonal':bad['rotation_world_to_cv'][0]*=2
   elif kind=='nan':bad['translation_world_to_cv'][0,0]=np.nan
   elif kind=='single_K':bad['intrinsics_px']=bad['intrinsics_px'][0]
   elif kind=='negative_fx':bad['intrinsics_px'][0,0,0]=-1
   elif kind=='missing_t':del bad['translation_world_to_cv']
   with self.subTest(kind=kind),self.assertRaises(ValueError):self.run_fit(q,uv,camera_sequence=bad)

 def test_depth_admission_uses_cv_depth_not_legacy_world_z(self):
  q,uv,cameras=self.case();cameras['translation_world_to_cv'][:,2]=-10.
  out=self.run_fit(q,uv,camera_sequence=cameras,fit_root=False,mutable_bones=[],max_nfev=1)
  self.assertLess(out['diagnostics']['camera_depth_min_world'],0.)
  self.assertFalse(out['diagnostics']['all_joints_in_front_of_camera'])
  self.assertFalse(out['diagnostics']['automatically_accepted'])

if __name__=='__main__':unittest.main()
