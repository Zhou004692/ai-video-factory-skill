"""Portable project state regressions; all media/state are disposable fixtures."""
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load():
    path = ROOT / 'scripts/recreation/project_store.py'
    spec = importlib.util.spec_from_file_location('portable_project_store', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.mkv'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=128x96:rate=10:duration=0.5', '-c:v', 'ffv1', str(self.source)],
            check=True, capture_output=True)
        self.m = load()
        self.project = self.root / 'project'
        self.roles = [{'id': 'lead', 'animal': 'husky', 'anchors': [
            {'frame_index': 1, 'bbox_xyxy': [5, 5, 60, 80]}]}]

    def init(self):
        return self.m.init_project(self.source, self.project, self.roles)

    def artifact(self, name='preview.bin'):
        path = self.project / name
        path.write_bytes(b'candidate CG media')
        return path

    def test_manifest_actual_frames_and_restart(self):
        state = self.init()
        self.assertEqual(state['schema'], 'animal-recreation-project-v1')
        self.assertEqual(state['source']['frame_count'], 5)
        self.assertEqual([f['index'] for f in state['source']['frames']], list(range(5)))
        self.assertEqual([f['pts_seconds'] for f in state['source']['frames']], [0., .1, .2, .3, .4])
        self.assertTrue(all(f['duration_seconds'] > 0 for f in state['source']['frames']))
        self.assertEqual(load().load_project(self.project), state)

    def test_source_mutation_and_manifest_tampering_rejected(self):
        self.init()
        data = json.loads((self.project / 'project.json').read_text())
        data['source']['frames'][0]['pts_seconds'] = 99
        (self.project / 'project.json').write_text(json.dumps(data))
        with self.assertRaises(ValueError): self.m.load_project(self.project)

    def test_source_bytes_mutation_rejected(self):
        self.init()
        with self.source.open('ab') as f: f.write(b'modified')
        with self.assertRaises(ValueError): self.m.load_project(self.project)

    def test_bad_crop_duplicate_roles_and_outside_anchors_rejected(self):
        for crop in [[0, 0, 129, 96], [-1, 0, 128, 96], [0, 0, True, 96], [0, 0, 0, 96]]:
            with self.subTest(crop=crop), self.assertRaises(ValueError):
                self.m.init_project(self.source, self.project, self.roles, crop)
        with self.assertRaises(ValueError):
            self.m.init_project(self.source, self.project, self.roles * 2)
        with self.assertRaises(ValueError):
            self.m.init_project(self.source, self.project,
                [{'id': 'x', 'animal': 'husky', 'anchors': [{'frame_index': 8, 'bbox_xyxy': [0, 0, 10, 10]}]}])

    def test_artifact_paths_and_stale_acceptance(self):
        self.init()
        outside = self.root / 'outside.bin'; outside.write_bytes(b'outside')
        for path in [outside, self.project / '../outside.bin', self.project / 'project.json']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.m.record_artifact(self.project, 'cg', path)
        path = self.artifact()
        item = self.m.record_artifact(self.project, 'cg', path)
        with self.assertRaises(ValueError): self.m.accept_artifact(self.project, 'cg', '0' * 64, 'accept')
        with self.assertRaises(ValueError): self.m.accept_artifact(self.project, 'cg', item['sha256'], '')
        accepted = self.m.accept_artifact(self.project, 'cg', item['sha256'], 'reviewed candidate')
        self.assertEqual(accepted['status'], 'accepted')
        path.write_bytes(b'replaced')
        with self.assertRaises(ValueError): self.m.get_artifact(self.project, 'cg')
        with self.assertRaises(ValueError): self.m.accept_artifact(self.project, 'cg', item['sha256'], 'accept')

    def test_symlink_escape_rejected(self):
        self.init()
        outside = self.root / 'outside'; outside.mkdir()
        (outside / 'x').write_bytes(b'x')
        link = self.project / 'link'
        try: link.symlink_to(outside, target_is_directory=True)
        except OSError: self.skipTest('Symlinks unavailable for this account')
        with self.assertRaises(ValueError): self.m.record_artifact(self.project, 'cg', link / 'x')

    def test_concurrent_writers_and_atomic_failure_resume(self):
        self.init()
        paths = [self.artifact(f'{i}.bin') for i in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda pair: self.m.record_artifact(self.project, str(pair[0]), pair[1]), enumerate(paths)))
        before = self.m.load_project(self.project)
        self.assertEqual(len(before['artifacts']), 8)
        with patch.object(self.m.os, 'replace', side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError): self.m.record_artifact(self.project, 'interrupted', paths[0])
        self.assertEqual(self.m.load_project(self.project), before)
        self.m.record_artifact(self.project, 'resumed', paths[0])
        self.assertEqual(len(self.m.load_project(self.project)['artifacts']), 9)

    def test_variable_frame_pts_not_invented_from_fps(self):
        variable = self.root / 'vfr.mkv'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=128x96:rate=10:duration=1', '-vf', r'select=eq(n\,0)+eq(n\,1)+eq(n\,8)',
            '-fps_mode', 'vfr', '-c:v', 'ffv1', str(variable)], check=True, capture_output=True)
        state = self.m.init_project(variable, self.project, self.roles)
        self.assertEqual([f['pts_seconds'] for f in state['source']['frames']], [0., .1, .8])

    def test_reviewed_role_correction_preserves_history_and_invalidates_acceptance(self):
        self.init(); path = self.artifact()
        item = self.m.record_artifact(self.project, 'cg_silent', path)
        self.m.accept_artifact(self.project, 'cg_silent', item['sha256'], 'reviewed')
        corrected = self.roles.copy()
        corrected = json.loads(json.dumps(corrected))
        corrected[0]['anchors'][0]['bbox_xyxy'][0] = 6
        before = self.m.load_project(self.project)
        after = self.m.update_roles(self.project, corrected, 'corrected lead anchor')
        self.assertNotEqual(after['binding_sha256'], before['binding_sha256'])
        self.assertEqual(after['source'], before['source'])
        artifact = after['artifacts']['cg_silent']
        self.assertEqual(artifact['status'], 'stale')
        self.assertNotIn('acceptance', artifact)
        self.assertEqual(artifact['history'][-1]['status'], 'accepted')
        self.assertTrue(path.exists())
        with self.assertRaises(ValueError): self.m.get_artifact(self.project, 'cg_silent')
        with self.assertRaises(ValueError): self.m.record_artifact(self.project, 'fake_accept', path, status='accepted')

    def test_stale_json_source_binding_cannot_be_recorded_as_current(self):
        state = self.init()
        path = self.project / 'tracks.json'
        path.write_text(json.dumps({'source_sha256': '0'*64, 'binding_sha256': state['binding_sha256']}))
        with self.assertRaises(ValueError): self.m.record_artifact(self.project, 'tracks', path)

    def test_state_cannot_be_written_over_source_file(self):
        inside = self.project / 'source.mkv'
        self.project.mkdir(); shutil.copy2(self.source, inside)
        self.m.init_project(inside, self.project, self.roles)
        with self.assertRaises(ValueError): self.m.record_artifact(self.project, 'cg', inside)

    def test_load_rejects_replaceable_skill_root_before_lock_creation(self):
        with self.assertRaises(ValueError): self.m.load_project(ROOT)
        with self.assertRaises(ValueError): self.m.load_project(ROOT / 'scripts' / 'recreation')

    def test_lock_is_bounded_and_recovers_after_process_death(self):
        self.init()
        with self.m._lock(self.project), patch.object(self.m, 'LOCK_TIMEOUT_SECONDS', .05):
            with self.assertRaises(TimeoutError): self.m.load_project(self.project)
        subprocess.run([sys.executable, '-c',
            'import os,sys;sys.path.insert(0,sys.argv[1]);import project_store as s;'
            'lock=s._lock(s._root(sys.argv[2]));lock.__enter__();os._exit(0)',
            str(ROOT / 'scripts/recreation'), str(self.project)], check=True, capture_output=True)
        self.assertEqual(self.m.load_project(self.project)['stage'], 'initialized')

    def test_batch_artifacts_publish_one_review_group_and_retain_history(self):
        self.init()
        self.assertTrue(callable(getattr(self.m, 'record_artifacts', None)), 'Atomic artifact batch API required')
        paths = {name: self.artifact(name+'.bin') for name in ['cg_silent', 'cg_audio', 'comparison', 'review']}
        first = self.m.record_artifacts(self.project, paths)
        self.assertEqual(set(first), set(paths))
        self.assertEqual(len({item['group_id'] for item in first.values()}), 1)
        self.assertEqual(len({item['binding_sha256'] for item in first.values()}), 1)
        next_paths = {name: self.artifact(name+'-v2.bin') for name in paths}
        second = self.m.record_artifacts(self.project, next_paths)
        self.assertNotEqual(second['cg_silent']['group_id'], first['cg_silent']['group_id'])
        for name, item in second.items():
            self.assertEqual(item['history'][-1]['group_id'], first[name]['group_id'])
            self.assertTrue(paths[name].exists())

    def test_batch_validation_or_atomic_replace_failure_publishes_nothing(self):
        self.init()
        self.assertTrue(callable(getattr(self.m, 'record_artifacts', None)), 'Atomic artifact batch API required')
        path = self.artifact(); before = self.m.load_project(self.project)
        with self.assertRaises(ValueError):
            self.m.record_artifacts(self.project, {'cg_silent': path, 'review': self.source})
        self.assertEqual(self.m.load_project(self.project), before)
        with patch.object(self.m.os, 'replace', side_effect=OSError('interrupted batch')):
            with self.assertRaises(OSError):
                self.m.record_artifacts(self.project, {'cg_silent': path, 'review': path})
        self.assertEqual(self.m.load_project(self.project), before)

    def test_new_track_or_observation_bytes_invalidate_downstream_review(self):
        self.init()
        tracks = self.artifact('tracks.bin'); observations = self.artifact('observations.bin')
        self.m.record_artifact(self.project, 'tracks', tracks)
        self.m.record_artifact(self.project, 'observations', observations)
        paths = {name: self.artifact(name+'.bin') for name in ['cg_silent', 'cg_audio', 'comparison', 'review']}
        group = self.m.record_artifacts(self.project, paths)
        self.m.accept_artifact(self.project, 'cg_silent', group['cg_silent']['sha256'], 'reviewed CG')
        same = self.artifact('tracks-same.bin')
        self.m.record_artifact(self.project, 'tracks', same)
        self.assertEqual(self.m.get_artifact(self.project, 'cg_silent')['status'], 'accepted')
        changed_observations = self.project / 'observations-new.bin'; changed_observations.write_bytes(b'new observations')
        self.m.record_artifact(self.project, 'observations', changed_observations)
        self.assertEqual(self.m.get_artifact(self.project, 'tracks')['status'], 'needs_review')
        with self.assertRaises(ValueError): self.m.get_artifact(self.project, 'cg_silent')
        stale = self.m.load_project(self.project)['artifacts']['cg_silent']
        self.assertEqual(stale['status'], 'stale')
        self.assertEqual(stale['history'][-1]['status'], 'accepted')
        changed_tracks = self.project / 'tracks-new.bin'; changed_tracks.write_bytes(b'new identity tracks')
        self.m.record_artifact(self.project, 'tracks', changed_tracks)
        with self.assertRaises(ValueError): self.m.get_artifact(self.project, 'observations')
        self.assertTrue(all(path.exists() for path in paths.values()))

    def test_observations_cannot_claim_an_old_tracks_sha(self):
        self.init(); tracks = self.artifact('tracks.bin')
        self.m.record_artifact(self.project, 'tracks', tracks)
        observations = self.project / 'observations.json'
        observations.write_text(json.dumps({'tracks_sha256': '0'*64}))
        with self.assertRaises(ValueError): self.m.record_artifact(self.project, 'observations', observations)

    def test_changed_upstream_bytes_invalidate_reads_even_before_registry_rerun(self):
        self.init(); tracks = self.artifact('tracks.bin')
        self.m.record_artifact(self.project, 'tracks', tracks)
        observations = self.artifact('observations.bin')
        self.m.record_artifact(self.project, 'observations', observations)
        tracks.write_bytes(b'changed externally')
        with self.assertRaises(ValueError): self.m.get_artifact(self.project, 'observations')

    def test_cg_read_checks_its_same_group_review_bytes(self):
        self.init()
        cg = self.artifact('cg.bin'); review = self.artifact('review.html')
        self.m.record_artifacts(self.project, {'cg_silent': cg, 'review': review})
        review.write_bytes(b'changed review')
        with self.assertRaises(ValueError): self.m.get_artifact(self.project, 'cg_silent')

if __name__ == '__main__': unittest.main()
