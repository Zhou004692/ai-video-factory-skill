"""Exercise the real local runner's crop, decoded coverage and source PTS."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import cv2
    import numpy as np
except ImportError:
    cv2=np=None

ROOT=Path(__file__).resolve().parents[1]

def load():
    directory=ROOT/'scripts/recreation'
    spec=importlib.util.spec_from_file_location('vitpose_input_runner',directory/'vitpose_inference.py')
    mod=importlib.util.module_from_spec(spec)
    sys.path.insert(0,str(directory))
    try:spec.loader.exec_module(mod)
    finally:sys.path.pop(0)
    return mod


@unittest.skipIf(cv2 is None,'Pose-stage NumPy/OpenCV required')
class RunnerInputTests(unittest.TestCase):
    def fixtures(self,directory,*,crop=(0,0,50,50),rows=1):
        source=directory/'source.bin';source.write_bytes(b'local-source-fixture')
        tracks=directory/'tracks.json'
        tracks.write_text(json.dumps(dict(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            fps=30,crop_xywh=crop,rows=[dict(source_frame_index=i,roles=[dict(id='actor',bbox_xyxy=[0,0,10,10])]) for i in range(rows)])),encoding='utf-8')
        manifest=directory/'manifest.json';manifest.write_text('{}',encoding='utf-8')
        return source,tracks,manifest,directory/'observations.json'

    def fake_model(self,images):
        class Model:
            model={'test_fixture':True}
            def __init__(self,manifest):pass
            def predict(self,image,boxes):
                images.append(image.copy())
                return [dict(test_fixture=True) for _ in boxes]
        return Model

    def fake_capture(self,frames,metadata_count=None):
        class Capture:
            def __init__(self):self.index=0;self.released=False
            def get(self,prop):
                if prop==cv2.CAP_PROP_FPS:return 30
                if prop==cv2.CAP_PROP_FRAME_COUNT:return len(frames) if metadata_count is None else metadata_count
                if prop==cv2.CAP_PROP_POS_MSEC:return max(0,self.index-1)*1000/30
                return 0
            def read(self):
                if self.index>=len(frames):return False,None
                frame=frames[self.index];self.index+=1;return True,frame
            def release(self):self.released=True
        return Capture()

    def test_bad_crops_publish_no_observations(self):
        invalid=[[-100,0,50,50],[0,-50,50,25],[0,0,0,50],[0,0,50,-10],
                 [0.,0,50,50],[True,0,50,50],[0,0,50.5,50],[0,0,50],
                 [180,0,50,50],[0,90,50,50]]
        for crop in invalid:
            with self.subTest(crop=crop),tempfile.TemporaryDirectory() as tmp:
                m=load();paths=self.fixtures(Path(tmp),crop=crop);cap=self.fake_capture([np.zeros((100,200,3),np.uint8)])
                with patch.object(m,'ViTPose',self.fake_model([])),patch.object(m.cv2,'VideoCapture',return_value=cap):
                    with self.assertRaises(ValueError):m.run(*paths)
                self.assertFalse(paths[-1].exists())

    def test_crop_uses_declared_top_left_and_exact_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=load();paths=self.fixtures(Path(tmp),crop=[150,50,50,50]);images=[]
            frame=np.zeros((100,200,3),np.uint8);frame[50:,150:]=123
            cap=self.fake_capture([frame])
            with patch.object(m,'ViTPose',self.fake_model(images)),patch.object(m.cv2,'VideoCapture',return_value=cap):r=m.run(*paths)
            self.assertTrue(np.all(images[0]==123));self.assertEqual(r['crop_xywh'],[150,50,50,50]);self.assertTrue(cap.released)

    def test_actual_decode_rejects_tracks_shorter_or_longer_than_video(self):
        for rows,count in [(1,2),(3,2)]:
            with self.subTest(rows=rows,count=count),tempfile.TemporaryDirectory() as tmp:
                m=load();paths=self.fixtures(Path(tmp),rows=rows)
                cap=self.fake_capture([np.zeros((100,200,3),np.uint8) for _ in range(count)],metadata_count=rows)
                with patch.object(m,'ViTPose',self.fake_model([])),patch.object(m.cv2,'VideoCapture',return_value=cap):
                    with self.assertRaises(ValueError):m.run(*paths)
                self.assertFalse(paths[-1].exists());self.assertTrue(cap.released)

    def test_bound_pts_accepts_different_average_fps_but_rejects_wrong_clock(self):
        for offset in [0, .1]:
            with self.subTest(offset=offset),tempfile.TemporaryDirectory() as tmp:
                m=load();paths=self.fixtures(Path(tmp),rows=2)
                payload=json.loads(paths[1].read_text(encoding='utf-8'))
                payload['fps']=15  # ffprobe average may differ from decoder nominal FPS.
                for i,row in enumerate(payload['rows']):row['timestamp_seconds']=i/30+offset
                paths[1].write_text(json.dumps(payload),encoding='utf-8')
                cap=self.fake_capture([np.zeros((100,200,3),np.uint8) for _ in range(2)])
                with patch.object(m,'ViTPose',self.fake_model([])),patch.object(m.cv2,'VideoCapture',return_value=cap),patch.object(m,'source_pts',return_value=[0,1/30]):
                    if offset:
                        with self.assertRaisesRegex(ValueError,'PTS'):m.run(*paths)
                        self.assertFalse(paths[-1].exists())
                    else:
                        result=m.run(*paths)
                        self.assertEqual([r['timestamp_seconds'] for r in result['rows']],[0,1/30])

    def test_invalid_or_partial_pts_is_not_treated_as_legacy_fps(self):
        for values in [[0,None],[0,0],[0,float('nan')],[False,1/30]]:
            with self.subTest(values=values),tempfile.TemporaryDirectory() as tmp:
                m=load();paths=self.fixtures(Path(tmp),rows=2)
                payload=json.loads(paths[1].read_text(encoding='utf-8'))
                for row,value in zip(payload['rows'],values):
                    if value is not None:row['timestamp_seconds']=value
                paths[1].write_text(json.dumps(payload),encoding='utf-8')
                cap=self.fake_capture([np.zeros((100,200,3),np.uint8) for _ in range(2)])
                with patch.object(m,'ViTPose',self.fake_model([])),patch.object(m.cv2,'VideoCapture',return_value=cap):
                    with self.assertRaises(ValueError):m.run(*paths)
                self.assertFalse(paths[-1].exists())

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg/ffprobe required')
    def test_real_nonzero_pts_is_preserved_while_decoder_normalizes_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);m=load();_,tracks,manifest,output=self.fixtures(directory,rows=3)
            source=directory/'offset.mkv'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=200x100:rate=10:duration=0.3',
                '-vf','setpts=PTS+2/TB','-fps_mode','passthrough','-c:v','ffv1',str(source)],check=True,capture_output=True)
            payload=json.loads(tracks.read_text(encoding='utf-8'))
            payload.update(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),fps=10)
            for i,row in enumerate(payload['rows']):row['timestamp_seconds']=2+i/10
            tracks.write_text(json.dumps(payload),encoding='utf-8')
            with patch.object(m,'ViTPose',self.fake_model([])):r=m.run(source,tracks,manifest,output)
            self.assertEqual([row['timestamp_seconds'] for row in r['rows']],[2,2.1,2.2])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg/ffprobe required')
    def test_real_variable_rate_video_uses_decoded_frames_and_preserves_pts(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);m=load();_,tracks,manifest,output=self.fixtures(directory,rows=5)
            source=directory/'variable-rate.mkv'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=200x100:rate=30:duration=1',
                '-vf',r'select=eq(n\,0)+eq(n\,1)+eq(n\,10)+eq(n\,11)+eq(n\,29)',
                '-fps_mode','vfr','-c:v','ffv1',str(source)],check=True,capture_output=True)
            payload=json.loads(tracks.read_text(encoding='utf-8'));payload['source_sha256']=hashlib.sha256(source.read_bytes()).hexdigest()
            cap=cv2.VideoCapture(str(source));payload['fps']=cap.get(cv2.CAP_PROP_FPS);estimated=cap.get(cv2.CAP_PROP_FRAME_COUNT);cap.release()
            tracks.write_text(json.dumps(payload),encoding='utf-8')
            decoded=json.loads(subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_frames',
                '-show_entries','frame=best_effort_timestamp_time','-of','json',str(source)],check=True,capture_output=True).stdout)
            expected=[float(f['best_effort_timestamp_time']) for f in decoded['frames']]
            self.assertEqual(len(expected),5)
            for row,pts in zip(payload['rows'],expected):row['timestamp_seconds']=pts
            tracks.write_text(json.dumps(payload),encoding='utf-8')
            with patch.object(m,'ViTPose',self.fake_model([])):r=m.run(source,tracks,manifest,output)
            self.assertEqual(len(r['rows']),5)
            for row,t in zip(r['rows'],expected):self.assertAlmostEqual(row['timestamp_seconds'],t,places=5)
            self.assertGreater(expected[2]-expected[1],.2)
            self.assertEqual(r['source_uploads'],0);self.assertEqual(r['paid_provider_calls'],0)

if __name__=='__main__':unittest.main()
