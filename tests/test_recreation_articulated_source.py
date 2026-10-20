"""Rigid body clusters and articulated limbs, independent of pose models."""
import importlib.util
from pathlib import Path
import unittest
try:
 import numpy as np
except ImportError:
 np=None

def load():
 p=Path(__file__).resolve().parents[1]/'scripts/recreation/articulated_source.py'
 if not p.exists():raise AssertionError('Articulated source rig implementation missing')
 s=importlib.util.spec_from_file_location('articulated_source',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

@unittest.skipUnless(np is not None,'optional NumPy fitting stage')
class ArticulatedSourceTests(unittest.TestCase):
 def source(self):
  return np.array([[0,1.2,0],[-.12,1.17,.02],[-.12,.65,.05],[-.12,.12,.09],
   [.13,1.17,.02],[.13,.65,.05],[.13,.12,.09],[0,1.45,0],[0,1.75,0],[0,1.88,0],[0,2.03,0],
   [.25,1.75,0],[.51,1.48,0],[.6,1.15,.03],[-.25,1.75,0],[-.51,1.48,0],[-.6,1.15,.03]])

 def model_state(self):
  m=load();rig=m.build_source_rig(self.source(),dict(width=.12,heel_length=.07,toe_length=.18,ankle_height=.06))
  I=np.eye(3);state=dict(root_world=np.array([.3,1.1,-4.]),pelvis_rotation=I,chest_rotation=I,
   limb_rotations=np.repeat(I[None],4,axis=0),hinge_angles=np.array([.5,.8,.4,.7]),
   foot_rotations=np.repeat(I[None],2,axis=0),cranial_directions=np.array([[0,1.,0],[0,1.,0]]))
  return m,rig,state

 def test_cluster_distances_are_invariant_under_independent_body_and_joint_rotations(self):
  m,rig,state=self.model_state();a=m.forward_source_rig(rig,**state)
  t=.8;R=np.array([[np.cos(t),0,np.sin(t)],[0,1,0],[-np.sin(t),0,np.cos(t)]])
  state['pelvis_rotation']=R;state['chest_rotation']=R.T;state['limb_rotations'][0]=R
  b=m.forward_source_rig(rig,**state)
  for group in [[0,1,4,7],[7,8,11,14]]:
   for i in group:
    for j in group:self.assertAlmostEqual(np.linalg.norm(a['joints_world'][i]-a['joints_world'][j]),np.linalg.norm(b['joints_world'][i]-b['joints_world'][j]),places=12)
  np.testing.assert_array_equal(b['joints_world'][0],state['root_world'])

 def test_every_bone_retains_its_length_when_hinges_and_root_move(self):
  m,rig,state=self.model_state();state['hinge_angles']=np.array([0.,2.5,.1,2.]);out=m.forward_source_rig(rig,**state)
  q=out['joints_world'];parents=np.array(rig['parents'])
  np.testing.assert_allclose(np.linalg.norm(q[1:]-q[parents[1:]],axis=1),rig['bone_lengths'],atol=1e-12)
  self.assertFalse(out['world_geometry_verified']);self.assertFalse(out['automatically_accepted'])

 def test_foot_support_is_measured_from_explicit_surface_vertices_and_respects_roll(self):
  m,rig,state=self.model_state();a=m.forward_source_rig(rig,**state)
  np.testing.assert_allclose(a['foot_vertices_world'][:,:,1].min(axis=1),a['joints_world'][[3,6],1]-.06)
  t=.7;R=np.array([[1,0,0],[0,np.cos(t),-np.sin(t)],[0,np.sin(t),np.cos(t)]])
  state['foot_rotations'][0]=R;b=m.forward_source_rig(rig,**state)
  self.assertGreater(abs(b['foot_vertices_world'][0,:,1].min()-a['foot_vertices_world'][0,:,1].min()),.02)
  np.testing.assert_allclose(a['joints_world'],b['joints_world'],atol=1e-12)
  self.assertEqual(b['foot_geometry_provenance'],'authored rigid proxy; not measured shoe or paw')

 def test_transform_does_not_mutate_reference_or_shape(self):
  m,rig,state=self.model_state();shape=np.array(rig['reference_xyz']);m.forward_source_rig(rig,**state)
  np.testing.assert_array_equal(rig['reference_xyz'],shape)
  self.assertEqual(rig['source_joint_schema'],'h36m17-derived-from-retained-wholebody133')

 def test_source_state_initialization_recovers_a_known_articulated_pose(self):
  m,rig,state=self.model_state();q=m.forward_source_rig(rig,**state)['joints_world']
  initialized=m.initialize_source_state(rig,q,foot_rotations=state['foot_rotations'])
  actual=m.forward_source_rig(rig,**initialized['state'])['joints_world']
  np.testing.assert_allclose(actual,q,atol=1e-12)
  self.assertFalse(initialized['observations_are_3d_truth'])

 def test_straight_limb_pole_uses_explicit_fallback_without_nan(self):
  m,rig,state=self.model_state();state['hinge_angles'][:]=0
  q=m.forward_source_rig(rig,**state)['joints_world']
  initialized=m.initialize_source_state(rig,q,foot_rotations=state['foot_rotations'],previous_state=state)
  self.assertEqual(initialized['pole_fallback_limbs'],[0,1,2,3])
  np.testing.assert_allclose(m.forward_source_rig(rig,**initialized['state'])['joints_world'],q,atol=1e-12)

 def test_rejects_scale_reflection_hyperextension_and_degenerate_reference(self):
  m,rig,state=self.model_state()
  for kind in ['scaled_rotation','reflection','negative_hinge','excess_hinge','nan']:
   bad={k:v.copy() for k,v in state.items()}
   if kind=='scaled_rotation':bad['pelvis_rotation']*=2
   elif kind=='reflection':bad['pelvis_rotation'][0]*=-1
   elif kind=='negative_hinge':bad['hinge_angles'][0]=-.1
   elif kind=='excess_hinge':bad['hinge_angles'][0]=np.pi+.1
   elif kind=='nan':bad['root_world'][0]=np.nan
   with self.subTest(kind=kind),self.assertRaises(ValueError):m.forward_source_rig(rig,**bad)
  q=self.source();q[3]=q[2]
  with self.assertRaises(ValueError):m.build_source_rig(q,dict(width=.12,heel_length=.07,toe_length=.18,ankle_height=.06))

if __name__=='__main__':unittest.main()
