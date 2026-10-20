"""Real CPU optical flow fixtures, without models or network access."""
import importlib.util
from pathlib import Path
import sys
import json
import shutil
import subprocess
import tempfile
import unittest

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = np = None

ROOT = Path(__file__).resolve().parents[1]

def load():
    directory = ROOT / 'scripts/recreation'
    spec = importlib.util.spec_from_file_location('portable_identity_tracks', directory / 'identity_tracks.py')
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(directory))
    try: spec.loader.exec_module(module)
    finally: sys.path.pop(0)
    return module

@unittest.skipIf(cv2 is None, 'Pose OpenCV/NumPy required')
class IdentityTrackTests(unittest.TestCase):
    def frames(self, count=6):
        rng = np.random.default_rng(12)
        patch = rng.integers(0, 255, (48, 32, 3), dtype=np.uint8)
        frames = []
        for i in range(count):
            frame = np.zeros((96, 160, 3), np.uint8)
            frame[20:68, 15+i*3:47+i*3] = patch
            frames.append(frame)
        return frames

    def role(self, frame=0, x=15):
        return {'id': 'actor-any-name', 'animal': 'husky', 'anchors': [
            {'frame_index': frame, 'bbox_xyxy': [x, 20, x+31, 67]}]}

    def test_translation_tracks_with_forward_backward_evidence(self):
        rows = load().track_frames(self.frames(), [self.role()])
        self.assertEqual(len(rows), 6)
        last = rows[-1]['roles'][0]
        self.assertIsNotNone(last['bbox_xyxy'])
        self.assertAlmostEqual(last['bbox_xyxy'][0], 30, delta=1)
        self.assertEqual(last['identity_provenance']['method'], 'anchored_sparse_lk_forward_backward')
        self.assertIn('forward_backward_median_px', last['identity_provenance'])
        self.assertTrue(last['needs_review'])

    def test_before_first_anchor_missing_and_later_anchor_corrects(self):
        role = self.role(2, 21)
        role['anchors'].append({'frame_index': 4, 'bbox_xyxy': [100, 20, 131, 67], 'provenance': 'user_reviewed'})
        rows = load().track_frames(self.frames(), [role])
        self.assertIsNone(rows[0]['roles'][0]['bbox_xyxy'])
        self.assertIn('before_first_anchor', rows[0]['roles'][0]['uncertainty'])
        self.assertEqual(rows[4]['roles'][0]['bbox_xyxy'], [100, 20, 131, 67])

    def test_low_texture_and_occlusion_missing_until_new_anchor(self):
        frames = self.frames()
        frames[2:] = [np.zeros_like(frames[0]) for _ in frames[2:]]
        rows = load().track_frames(frames, [self.role()])
        self.assertIsNone(rows[2]['roles'][0]['bbox_xyxy'])
        self.assertTrue(rows[2]['roles'][0]['uncertainty'])
        rows = load().track_frames([np.zeros_like(frames[0])] * 3, [self.role()])
        self.assertIsNone(rows[1]['roles'][0]['bbox_xyxy'])

    def test_overlapping_identities_are_missing_not_silently_swapped(self):
        role = self.role(); other = self.role(); other['id'] = 'other'
        rows = load().track_frames(self.frames(), [role, other])
        self.assertTrue(all(r['bbox_xyxy'] is None for r in rows[1]['roles']))
        self.assertTrue(all('identity_overlap_ambiguous' in r['uncertainty'] for r in rows[1]['roles']))

    def test_output_uses_complete_existing_vitpose_schema(self):
        rows = load().track_frames(self.frames(), [self.role()])
        self.assertEqual([row['source_frame_index'] for row in rows], list(range(6)))
        self.assertTrue(all({'id', 'bbox_xyxy', 'identity_provenance', 'uncertainty'} <= set(r) for row in rows for r in row['roles']))

    def test_duplicate_anchors_are_rejected_instead_of_silently_discarded(self):
        role = self.role(); role['anchors'] *= 2
        with self.assertRaises(ValueError): load().track_frames(self.frames(), [role])

    @unittest.skipUnless(shutil.which('ffprobe'), 'ffprobe required')
    def test_actual_project_cli_preserves_pts_registers_artifact_and_refuses_overwrite(self):
        from tests.test_recreation_project_store import load as load_store
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); source = directory / 'moving.mkv'
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*'FFV1'), 10, (160, 96))
            self.assertTrue(writer.isOpened())
            try:
                for frame in self.frames(): writer.write(frame)
            finally: writer.release()
            store = load_store(); project = directory / 'project'
            state = store.init_project(source, project, [self.role()])
            output = project / 'tracks.json'
            command = [sys.executable, str(ROOT / 'scripts/recreation/identity_tracks.py'),
                '--project', str(project), '--output', str(output)]
            subprocess.run(command, check=True, capture_output=True)
            report = json.loads(output.read_text())
            self.assertEqual(len(report['rows']), state['source']['frame_count'])
            self.assertEqual([r['timestamp_seconds'] for r in report['rows']],
                [f['pts_seconds'] for f in state['source']['frames']])
            self.assertEqual(store.get_artifact(project, 'tracks')['sha256'], store.file_sha256(output))
            before = output.read_bytes()
            repeated = subprocess.run(command, capture_output=True)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual(output.read_bytes(), before)

if __name__ == '__main__': unittest.main()
