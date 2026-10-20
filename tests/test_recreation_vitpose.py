"""ViTPose migration must preserve whole-body observations and source timing."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]

def load(name):
    path=ROOT/'scripts/recreation'/f'{name}.py'
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

class WholeBodyContractTests(unittest.TestCase):
    def test_preserves_all_parts_and_marks_h36m_derived_points(self):
        mod=load('pose_schema');xy=[[float(i),float(i+1)] for i in range(133)];scores=[.9]*133
        out=mod.wholebody_record(xy,scores)
        self.assertEqual(out['xy133'],xy);self.assertEqual(out['scores133'],scores)
        self.assertEqual(len(out['face_xy68']),68);self.assertEqual(len(out['hand_left_xy21']),21)
        self.assertEqual(len(out['hand_right_xy21']),21);self.assertEqual(len(out['feet_xy6']),6)
        self.assertEqual(out['h36m17'][1][:2],xy[12]);self.assertEqual(out['h36m17'][4][:2],xy[11])
        self.assertEqual(out['h36m17'][10][:2],[(xy[1][i]+xy[2][i])/2 for i in range(2)])
        self.assertEqual(out['h36m_derived_indices'],[0,7,8,10])
        self.assertNotIn('head_top_observed',out)
        self.assertEqual(xy,[[float(i),float(i+1)] for i in range(133)])

    def test_rejects_17_point_checkpoint_and_nonfinite_observations(self):
        mod=load('pose_schema')
        with self.assertRaisesRegex(ValueError,'133'):mod.wholebody_record([[0,0]]*17,[1]*17)
        xy=[[0.,0.]]*133;xy[20]=[float('nan'),2]
        with self.assertRaisesRegex(ValueError,'finite'):mod.wholebody_record(xy,[1]*133)

    def test_manifest_is_local_hash_bound_and_declares_decoder(self):
        mod=load('pose_schema')
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'weight.onnx';p.write_bytes(b'test-placeholder-not-a-real-model')
            m={'family':'vitpose','schema':'coco_wholebody133','onnx':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'input_size_wh':[192,256],'decoder':'mmpose-msra-default','source_url':'https://example.com/test.onnx'}
            self.assertEqual(mod.validate_model_manifest(m)['keypoints'],133)
            m['sha256']='0'*64
            with self.assertRaisesRegex(ValueError,'SHA'):mod.validate_model_manifest(m)
            m['onnx']='https://example.com/remote.onnx'
            with self.assertRaisesRegex(ValueError,'local'):mod.validate_model_manifest(m)

class RetargetDirectionTests(unittest.TestCase):
    def test_fixed_bones_do_not_turn_a_small_depth_crossing_into_50_degrees(self):
        mod=load('retarget')
        # Actual S1 right-thigh directions before the old F14->F15 fit.
        vectors=[[-.087635234,.99610865,.009355942],[-.087307937,.99588746,.024198329]]
        poses=[]
        for vec in vectors:
            q=[[0.,0.,0.]]
            for j,p in enumerate(mod.H36M_PARENTS[1:],1):q.append([q[p][k]+(vec[k] if j==2 else [0.,.4,0.][k]) for k in range(3)])
            poses.append(q)
        lengths=[.4]*16
        out=[mod.fixed_shape_fk(q,lengths) for q in poses]
        import math
        dirs=[[out[n][2][k]-out[n][1][k] for k in range(3)] for n in range(2)]
        dot=sum(dirs[0][k]*dirs[1][k] for k in range(3))/.16
        self.assertLess(math.degrees(math.acos(max(-1,min(1,dot)))),2)
        for q in out:
            for j,p in enumerate(mod.H36M_PARENTS[1:],1):self.assertAlmostEqual(math.dist(q[j],q[p]),.4,places=12)

    def test_degenerate_segment_is_rejected_and_root_is_preserved(self):
        mod=load('retarget')
        with self.assertRaisesRegex(ValueError,'degenerate'):mod.fixed_shape_fk([[0,0,0]]*17,[1]*16)
        q=[[4.,5.,6.]]
        for p in mod.H36M_PARENTS[1:]:q.append([q[p][0],q[p][1]+1,q[p][2]])
        out=mod.fixed_shape_fk(q,[.5]*16)
        self.assertEqual(out[0],q[0]);self.assertEqual(q[2][1],7.)

class ContactIKTests(unittest.TestCase):
 def test_reachable_foot_target_and_bend_hint_preserve_both_lengths(self):
  two_bone_ik=load('retarget').two_bone_ik
  import math
  hip=[0,1,0];knee=[0,.5,.4];ankle=[0,0,0];target=[.1,.1,.05]
  r=two_bone_ik(hip,knee,ankle,target,[0,.5,1])
  self.assertTrue(r['reachable']);self.assertLess(math.dist(r['tip'],target),1e-9)
  self.assertAlmostEqual(math.dist(hip,r['mid']),math.dist(hip,knee),places=10)
  self.assertAlmostEqual(math.dist(r['mid'],r['tip']),math.dist(knee,ankle),places=10)
  self.assertGreater(r['mid'][2],0)
 def test_unreachable_target_is_reported_without_stretching(self):
  two_bone_ik=load('retarget').two_bone_ik
  import math
  r=two_bone_ik([0,1,0],[0,.5,.4],[0,0,0],[0,-5,0],[0,.5,1])
  self.assertFalse(r['reachable']);self.assertGreater(r['target_error'],1)
  self.assertAlmostEqual(math.dist([0,1,0],r['mid']),math.sqrt(.41),places=9)

class HeadContinuityTests(unittest.TestCase):
 def test_angle_wrap_and_rate_limit_preserve_raw_guide(self):
  import math
  raw=[3.12,-3.12,-2.1,-2.0];copy=raw[:]
  out=load('retarget').bounded_angle_guide(raw,.2)
  self.assertEqual(raw,copy);self.assertEqual(len(out),4)
  self.assertTrue(all(abs(b-a)<=.2+1e-12 for a,b in zip(out,out[1:])))
  self.assertGreater(min(out),2.)

if __name__=='__main__':unittest.main()
