"""Run with the optional pose dependencies; inspect-only does not need them."""
import sys
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts/recreation'))
try:
 import numpy as np
 import cv2
except ImportError:
 np=cv2=None

@unittest.skipIf(np is None or cv2 is None,'optional pose dependencies absent')
class DemoNumericsTests(unittest.TestCase):
 def test_msra_heatmap_quarter_offset_and_source_coordinates(self):
  from vitpose_inference import decode_heatmaps
  h=np.zeros((1,133,64,48),np.float32);h[:,:,30,20]=1;h[:,:,30,21]=.3;h[:,:,30,19]=.1
  p,s=decode_heatmaps(h,np.array([[250,200]],np.float32),np.array([[1.2,1.6]],np.float32))
  np.testing.assert_allclose(p[0,0],[231.25,190],atol=1e-5);self.assertEqual(s.shape,(1,133,1))
 def test_preprocess_matches_declared_inclusive_bbox_and_RGB(self):
  from vitpose_inference import preprocess
  im=np.zeros((256,192,3),np.uint8);im[:]=[10,20,30]
  x,c,s=preprocess(im,[0,0,191,255],[192,256])
  self.assertEqual(x.shape,(1,3,256,192));np.testing.assert_equal(c,[96,128])
  np.testing.assert_allclose(x[0,:,128,96],(np.array([30,20,10])/255-np.array([.485,.456,.406]))/np.array([.229,.224,.225]),atol=1e-6)
 def test_flip_task_pairs_are_bijective_and_preserve_all_133(self):
  from vitpose_inference import flip_heatmaps
  h=np.arange(133*4*6,dtype=np.float32).reshape(1,133,4,6)
  out=flip_heatmaps(h,shift=False);back=flip_heatmaps(out,shift=False)
  np.testing.assert_equal(back,h);self.assertEqual(out.shape,h.shape)
  np.testing.assert_equal(out[:,1],h[:,2,:,::-1])

if __name__=='__main__':unittest.main()
