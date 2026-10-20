"""Portable preview contract; fixtures are synthetic observations, not model claims."""
import copy
import array
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/recreation'))
import project_store as store
SPEC = importlib.util.spec_from_file_location('portable_render', ROOT/'scripts/recreation/render_preview.py')
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


def fixture(root):
    source = root/'source.bin'
    source.write_bytes(b'synthetic source binding')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    points = [[100., 100.] for _ in range(133)]
    for i, p in {0:[100,45],1:[95,42],2:[105,42],5:[85,80],6:[115,80],
                 7:[70,110],8:[130,110],9:[60,135],10:[140,135],
                 11:[90,140],12:[110,140],13:[90,175],14:[110,175],
                 15:[90,210],16:[110,210]}.items():
        points[i] = p
    frames = [{'index':i, 'pts_seconds':pts, 'duration_seconds':duration}
              for i, (pts,duration) in enumerate([(2.,.04),(2.04,.08),(2.12,.04)])]
    project = {'schema':'animal-recreation-project-v1', 'project_id':'test',
               'source':{'path':str(source), 'sha256':digest, 'width':320, 'height':240,
                         'frame_count':3, 'fps':25., 'crop_xywh':[0,0,320,240],
                         'frames':frames, 'audio_stream_count':0},
               'roles':[{'id':'one', 'animal':'husky'}, {'id':'two','animal':'golden'}]}
    observations = {'source_sha256':digest, 'crop_xywh':[0,0,320,240],
        'keypoint_schema':'coco_wholebody133', 'rows':[
            {'source_frame_index':f['index'], 'timestamp_seconds':f['pts_seconds'],
             'roles':[{'id':role['id'], 'xy133':copy.deepcopy(points), 'scores133':[1.]*133}
                      for role in project['roles']]} for f in frames]}
    return project, observations


class PortableRenderContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project, self.observations = fixture(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def scene(self):
        return render.prepare_scene(self.project, self.observations)

    def test_rejects_unsupported_animal_and_unimplemented_world_mode(self):
        self.project['roles'][0]['animal'] = 'cat'
        with self.assertRaisesRegex(ValueError, 'animal'):
            self.scene()
        self.project['roles'][0]['animal'] = 'husky'
        with self.assertRaisesRegex(ValueError, 'screen-space'):
            render.prepare_scene(self.project, self.observations, {'mode':'world-space'})

    def test_source_mutation_and_observation_binding_are_rejected(self):
        self.observations['source_sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'source'):
            self.scene()
        self.observations['source_sha256'] = self.project['source']['sha256']
        Path(self.project['source']['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'source'):
            self.scene()

    def test_all_native_pts_and_frame_coverage_are_required(self):
        scene = self.scene()
        self.assertEqual([f['pts_seconds'] for f in scene['frames']], [2.,2.04,2.12])
        self.assertAlmostEqual(scene['duration_seconds'], .16)
        self.observations['rows'][1]['timestamp_seconds'] = 2.08
        with self.assertRaisesRegex(ValueError, 'timestamp'):
            self.scene()
        self.observations['rows'].pop()
        with self.assertRaisesRegex(ValueError, 'coverage'):
            self.scene()

    def test_missing_limbs_are_reported_and_never_filled_or_frozen(self):
        self.observations['rows'][1]['roles'][0]['scores133'][9] = 0.
        scene = self.scene()
        actor = scene['frames'][1]['roles'][0]
        self.assertIsNone(actor['points'][9])
        self.assertFalse(actor['visibility']['arm_left'])
        self.assertTrue(scene['frames'][0]['roles'][0]['visibility']['arm_left'])
        self.assertTrue(any(x['frame_index']==1 and x['role_id']=='one'
                            for x in scene['observation_gaps']))
        self.assertFalse(scene['depth_recovered'])
        self.assertFalse(scene['contact_verified'])

    def test_crop_role_identity_and_finite_observations_are_required(self):
        self.observations['crop_xywh'] = [0,0,100,100]
        with self.assertRaisesRegex(ValueError, 'crop'):
            self.scene()
        self.observations['crop_xywh'] = self.project['source']['crop_xywh']
        self.observations['rows'][0]['roles'].append(self.observations['rows'][0]['roles'][0])
        with self.assertRaisesRegex(ValueError, 'role'):
            self.scene()
        self.observations['rows'][0]['roles'].pop()
        self.observations['rows'][0]['roles'][0]['xy133'][9][0] = float('nan')
        with self.assertRaisesRegex(ValueError, '[Ff]inite'):
            self.scene()

    def test_browser_and_asset_resource_report_is_real_and_hash_bound(self):
        report = render.resource_report({'browser_path':str(self.root/'absent.exe')})
        self.assertFalse(report['ready'])
        self.assertIn('browser', report['missing'])
        self.assertIn('vendor/three.module.js', report['assets'])
        self.assertEqual(len(report['assets']['rig.js']),64)
        report = render.resource_report({'assets_dir':str(self.root/'absent-assets')})
        self.assertFalse(report['ready'])
        self.assertTrue(any('rig.js' in x for x in report['missing']))
        copied = self.root/'assets'
        shutil.copytree(render.ASSETS, copied)
        (copied/'rig.js').write_text('modified rig',encoding='utf-8')
        report = render.resource_report({'assets_dir':str(copied)})
        self.assertIn('rig.js:sha256-mismatch',report['missing'])

    def test_absent_role_is_explicit_and_not_previous_pose(self):
        self.observations['rows'][1]['roles'][0] = {'id':'one','missing_observation':True}
        scene = self.scene()
        self.assertFalse(scene['frames'][1]['roles'][0]['visibility']['body'])
        self.assertEqual(scene['frames'][1]['roles'][0]['points'],[None]*133)
        self.assertTrue(scene['frames'][2]['roles'][0]['visibility']['body'])
        self.assertEqual(len({r['fixed_pixels_per_unit'] for r in scene['roles']}),1)

    def test_short_forelimbs_have_one_source_specific_projected_shape(self):
        scene=self.scene();role=scene['roles'][0]
        shape=role.get('fixed_projected_shape')
        self.assertIsNotNone(shape,'Default template arm length is not the final staged rig shape')
        scale=role['fixed_pixels_per_unit']
        self.assertAlmostEqual(shape['limbs']['arm_left']['upper'],((85-70)**2+(80-110)**2)**.5/scale)
        self.assertAlmostEqual(shape['limbs']['arm_left']['lower'],((70-60)**2+(110-135)**2)**.5/scale)
        self.assertLess(shape['limbs']['arm_left']['upper'],1.)
        self.assertEqual(shape['provenance'],'current-source reliable 2D segment medians; fixed screen-space staging estimate, not metric anatomy')
        self.assertFalse(shape['per_frame_stretch_allowed'])

    def test_fixed_side_mapping_uses_torso_local_shoulders_when_inverted(self):
        q=self.observations['rows'][0]['roles'][0]['xy133']
        for i,point in {5:[175,130],6:[145,130],11:[170,80],12:[150,80]}.items():q[i]=point
        role=self.scene()['roles'][0]
        self.assertTrue(role['source_side_mapping']['left_first'])
        self.assertFalse(role['source_side_mapping']['ambiguous'])
        q[5]=[160,115];q[6]=[160,145]
        self.assertTrue(self.scene()['roles'][0]['source_side_mapping']['ambiguous'])
        self.assertIsNone(self.scene()['roles'][0]['source_side_mapping']['left_first'])

    def test_unrepresentable_tiny_segments_are_not_admitted_as_mesh_shape(self):
        for row in self.observations['rows']:
            q=row['roles'][0]['xy133'];q[7]=[q[5][0]+.0001,q[5][1]];q[9]=[q[5][0]+.0002,q[5][1]]
        shape=self.scene()['roles'][0]['fixed_projected_shape']
        self.assertFalse(shape['limbs']['arm_left']['available'])
        self.assertEqual(shape['minimum_mesh_segment_length'],.001)

    @unittest.skipUnless(shutil.which('node'),'Node required for projected IK primitive')
    def test_folded_unequal_chain_keeps_observed_hint_branch(self):
        module=(render.ASSETS/'projected-limbs.mjs').as_uri()
        script='import {solveProjectedChain as solve} from '+json.dumps(module)+'; console.log(JSON.stringify(solve([0,0,0],[0,1,0],[0,0,0],1,2)));'
        solved=json.loads(subprocess.check_output(['node','--input-type=module','-e',script],text=True))
        self.assertEqual(solved['joint'],[0,1,0])
        self.assertEqual(solved['tip'],[0,-1,0])

    def bound_project(self, directory):
        source=self.root/(directory+'-source.mp4')
        subprocess.run(['ffmpeg','-v','error','-n','-f','lavfi','-i',
            'testsrc2=size=320x240:rate=25:duration=0.12','-c:v','libx264',
            '-bf','0','-pix_fmt','yuv420p',str(source)],check=True,capture_output=True)
        project=self.root/directory
        roles=copy.deepcopy(self.project['roles'])
        for role in roles:role['anchors']=[{'frame_index':0,'bbox_xyxy':[10,10,200,220]}]
        state=store.init_project(source,project,roles)
        tracks=project/'tracks.json'
        tracks.write_text(json.dumps({'source_sha256':state['source']['sha256'],
                         'binding_sha256':state['binding_sha256']}),encoding='utf8')
        registered=store.record_artifact(project,'tracks',tracks)
        observations=copy.deepcopy(self.observations)
        observations['source_sha256']=state['source']['sha256']
        observations['binding_sha256']=state['binding_sha256']
        observations['tracks_sha256']=registered['sha256']
        for row,frame in zip(observations['rows'],state['source']['frames']):
            row['timestamp_seconds']=frame['pts_seconds']
        obs=project/'observations.json';obs.write_text(json.dumps(observations),encoding='utf8')
        store.record_artifact(project,'observations',obs)
        return project,obs,state

    def test_direct_api_rejects_observations_after_reviewed_role_correction(self):
        project,obs,state=self.bound_project('role-corrected')
        roles=copy.deepcopy(state['roles']);roles[0]['anchors'][0]['bbox_xyxy'][0]+=1
        store.update_roles(project,roles,'correct source role anchor')
        output=project/'rejected-preview'
        with self.assertRaisesRegex(ValueError,'invalidated|stale'):
            render.render_preview(project,obs,output,settings={'browser_path':'missing-browser'})
        self.assertFalse(output.exists())

    def test_direct_api_rejects_old_unregistered_observation_path(self):
        project,old,state=self.bound_project('new-observations')
        new=project/'observations-new.json'
        payload=json.loads(old.read_text());payload['note']='new current observations'
        new.write_text(json.dumps(payload),encoding='utf8')
        store.record_artifact(project,'observations',new)
        output=project/'rejected-preview'
        with self.assertRaisesRegex(ValueError,'current observations'):
            render.render_preview(project,old,output,settings={'browser_path':'missing-browser'})
        self.assertFalse(output.exists())

    @unittest.skipUnless(os.environ.get('RECREATION_RENDER_INTEGRATION')=='1',
                         'set RECREATION_RENDER_INTEGRATION=1 for owned-browser/FFmpeg smoke')
    def test_actual_vfr_cpu_canvas_and_audio_review(self):
        source = self.root/'actual-source.mp4'
        subprocess.run(['ffmpeg','-v','error','-n','-f','lavfi','-i',
            'testsrc2=size=320x240:rate=25:duration=0.16','-f','lavfi','-i',
            'sine=frequency=440:sample_rate=48000:duration=0.16',
            '-vf','select=not(eq(n\\,2))','-fps_mode','passthrough',
            '-c:v','libx264','-bf','0','-pix_fmt','yuv420p','-af','asetpts=PTS+0.04/TB',
            '-c:a','aac',str(source)],
            check=True,capture_output=True)
        probe = render._probe(source)
        native = [f for f in probe['frames'] if f['media_type']=='video']
        frames = [{'index':i,'pts_seconds':float(f['best_effort_timestamp_time']),
                   'duration_seconds':float(f.get('duration_time',f.get('pkt_duration_time')))}
                  for i,f in enumerate(native)]
        self.assertEqual([round(f['pts_seconds'],2) for f in frames],[0.,.04,.12])
        project=self.root/'actual-project'
        roles=copy.deepcopy(self.project['roles'])
        for role in roles:role['anchors']=[{'frame_index':0,'bbox_xyxy':[10,10,200,220]}]
        self.project=store.init_project(source,project,roles)
        tracks=project/'tracks.json'
        tracks.write_text(json.dumps({'source_sha256':self.project['source']['sha256'],
                         'binding_sha256':self.project['binding_sha256']}),encoding='utf8')
        track_artifact=store.record_artifact(project,'tracks',tracks)
        self.observations['source_sha256']=self.project['source']['sha256']
        self.observations['binding_sha256']=self.project['binding_sha256']
        self.observations['tracks_sha256']=track_artifact['sha256']
        for row,frame in zip(self.observations['rows'],frames):
            row['timestamp_seconds']=frame['pts_seconds']
        obs=project/'observations.json'
        obs.write_text(json.dumps(self.observations),encoding='utf8')
        store.record_artifact(project,'observations',obs)
        report=render.render_preview(project,obs,project/'preview',settings={'width':320})
        self.assertTrue(report['video']['native_pts_verified'])
        self.assertEqual(report['video']['frame_count'],3)
        self.assertAlmostEqual(report['video']['duration_seconds'],.16,places=3)
        self.assertIn('SwiftShader',report['render']['renderer'])
        self.assertFalse(report['render']['bone_lengths_changed_per_frame'])
        for frame in report['render']['frame_records']:
            for actor in frame['roles']:
                for limb in actor['limb_geometry'].values():
                    if not limb['visible']:continue
                    self.assertAlmostEqual(limb['actual_mesh_upper_length'],limb['fixed_upper_length'],places=6)
                    self.assertAlmostEqual(limb['actual_mesh_lower_length'],limb['fixed_lower_length'],places=6)
                elbow=[v['error_pixels'] for v in actor['landmark_residuals_pixels'] if v['landmark']=='elbow']
                self.assertLess(max(elbow),.01,'Short source arms must not acquire outboard default-template elbows')
        for name,path in report['artifacts'].items():
            self.assertEqual(render.sha256(path),report['artifact_sha256'][name])
        audio=render._probe(report['artifacts']['cg_audio'])
        self.assertEqual(sum(s['codec_type']=='audio' for s in audio['streams']),1)
        def onset(path):
            pcm=subprocess.check_output(['ffmpeg','-v','error','-copyts','-i',str(path),
                '-map','0:a:0','-af','aresample=async=0:first_pts=0',
                '-ac','1','-ar','48000','-f','s16le','pipe:1'])
            samples=array.array('h');samples.frombytes(pcm)
            return next(i for i,value in enumerate(samples) if abs(value)>1000)/48000
        # Original audio starts after video. Re-encoding must preserve that
        # offset, rather than zeroing the audio stream independently.
        self.assertAlmostEqual(onset(source),onset(report['artifacts']['cg_audio']),delta=.005)
        self.assertAlmostEqual(onset(source),onset(report['artifacts']['comparison']),delta=.005)
        self.assertEqual(report['paid_provider_calls'],0)
        self.project['source']['frames'][-1]['duration_seconds'] = 1.
        (project/'project.json').write_text(json.dumps(self.project),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'modified'):
            render.render_preview(project,obs,project/'padded-preview',settings={'width':320})
        self.assertFalse((project/'padded-preview').exists())

    def test_output_directory_cannot_overwrite_or_escape_project(self):
        (self.root/'project.json').write_text(json.dumps(self.project), encoding='utf-8')
        observations = self.root/'observations.json'
        observations.write_text(json.dumps(self.observations), encoding='utf-8')
        output = self.root/'existing'
        output.mkdir()
        with self.assertRaisesRegex(ValueError, 'exist'):
            render.render_preview(self.root, observations, output)
        with self.assertRaisesRegex(ValueError, 'project'):
            render.render_preview(self.root, observations, self.root.parent/'escaped')


if __name__ == '__main__':
    unittest.main()
