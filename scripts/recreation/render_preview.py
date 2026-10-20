"""Offline, isolated-browser canine animation reference and timestamped review.

Default output is explicitly screen-space 2.5D. Depth, floor/contact, emotion and
physical camera motion are not recovered. Missing landmarks remain missing.
Only the Python standard library, Node 22+, FFmpeg and a local browser are used.
"""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parents[1]/'assets'/'recreation'
MODE = 'screen-space-2.5d'


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _browser(settings):
    explicit = settings.get('browser_path') or os.environ.get('CHROME_BIN')
    if explicit:
        path = Path(explicit).expanduser()
        return str(path.resolve()) if path.is_file() else None
    candidates = [
        Path(os.environ.get('PROGRAMFILES', 'C:/Program Files'))/'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe',
        Path(os.environ.get('LOCALAPPDATA', ''))/'Google/Chrome/Application/chrome.exe',
        Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        Path('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'),
    ]
    for name in ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge']:
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    return next((str(p.resolve()) for p in candidates if p.is_file()), None)


def resource_report(settings=None):
    """Read-only, hash-verified readiness; never download or start a browser."""
    settings = settings or {}
    base = Path(settings.get('assets_dir', ASSETS)).resolve()
    required = ['rig.js', 'animal-body.js', 'face-geometry.js', 'mouth-shapes.js',
                'head-binding.js', 'scene.mjs', 'projected-limbs.mjs', 'vendor/three.module.js',
                'vendor/three.core.js', 'vendor/LICENSE', 'ASSET-NOTICE.md']
    missing, assets = [], {}
    manifest_path = base/'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        manifest = {}
        missing.append('manifest.json')
    for relative in required:
        path = base/relative
        if not path.is_file():
            missing.append(relative)
            continue
        digest = sha256(path)
        assets[relative] = digest
        if manifest.get('files', {}).get(relative) != digest:
            missing.append(relative+':sha256-mismatch')
    browser = _browser(settings)
    if not browser:
        missing.append('browser')
    if not (HERE/'render_scene.mjs').is_file():
        missing.append('render_scene.mjs')
    if not shutil.which('node'):
        missing.append('node')
    for name in ['ffmpeg', 'ffprobe']:
        if not shutil.which(name):
            missing.append(name)
    return {'ready':not missing, 'missing':missing, 'browser_path':browser,
            'assets_dir':str(base), 'assets':assets,
            'three_version':manifest.get('three_version'), 'mode':MODE}


def prepare_scene(project, observations, settings=None):
    """Validate complete source-bound observations and retain every native PTS.

    Geometry receives reliable landmarks only. One fixed role scale is measured
    from the clip's median shoulder-to-hip distance; no bone stretching, temporal
    interpolation, arbitrary depth, autonomous gestures, or hold-last-pose.
    """
    settings = settings or {}
    if settings.get('mode', MODE) != MODE:
        raise ValueError('Only screen-space-2.5d is implemented; world-space needs a separate calibrated contract')
    threshold = settings.get('confidence_threshold', .2)
    if not _finite(threshold) or not 0 <= threshold <= 1:
        raise ValueError('confidence_threshold must be in [0,1]')
    if project.get('schema') != 'animal-recreation-project-v1':
        raise ValueError('Unsupported project schema')
    source = project['source']
    if sha256(source['path']) != source['sha256'] or observations.get('source_sha256') != source['sha256']:
        raise ValueError('Observations/project/source SHA256 binding mismatch')
    crop = source['crop_xywh']
    if (not isinstance(crop, list) or len(crop) != 4 or any(type(v) is not int for v in crop)
            or min(crop[:2]) < 0 or min(crop[2:]) <= 0
            or crop[0]+crop[2] > source['width'] or crop[1]+crop[3] > source['height']):
        raise ValueError('Invalid source crop')
    if observations.get('crop_xywh') != crop:
        raise ValueError('Observation crop differs from project crop')
    if observations.get('keypoint_schema') != 'coco_wholebody133':
        raise ValueError('coco_wholebody133 observations required')
    roles = project['roles']
    ids = [r['id'] for r in roles]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('Unique nonempty role identities required')
    for role in roles:
        if role.get('animal') not in ('husky', 'golden'):
            raise ValueError('Unsupported animal; authored assets support husky and golden only')
    native = source['frames']
    rows = observations.get('rows', [])
    if not native or len(native) != source['frame_count'] or len(rows) != len(native):
        raise ValueError('Complete source frame coverage required')
    gaps, frames, torso_lengths = [], [], {role:[] for role in ids}
    for index, (frame, row) in enumerate(zip(native, rows)):
        pts, duration = frame.get('pts_seconds'), frame.get('duration_seconds')
        if (frame.get('index') != index or row.get('source_frame_index') != index
                or not _finite(pts) or not _finite(duration) or duration <= 0):
            raise ValueError('Native frame indexes, finite PTS and positive durations required')
        if index and pts <= native[index-1]['pts_seconds']:
            raise ValueError('Frame PTS must be strictly increasing')
        if not _finite(row.get('timestamp_seconds')) or abs(row['timestamp_seconds']-pts) > .002:
            raise ValueError('Observation timestamp differs from native source frame PTS')
        row_roles = row.get('roles', [])
        row_ids = [r['id'] for r in row_roles]
        if len(row_ids) != len(set(row_ids)) or set(row_ids) != set(ids):
            raise ValueError('Every frame must explicitly contain all known role identities, including missing roles')
        by_id = {r['id']:r for r in row_roles}
        actors = []
        for role_id in ids:
            role = by_id[role_id]
            raw = role.get('xy133')
            scores = role.get('scores133')
            if role.get('missing_observation') is True:
                points = [None]*133
            else:
                if (not isinstance(raw, list) or len(raw) != 133 or not isinstance(scores, list) or len(scores) != 133
                        or any(not isinstance(p, list) or len(p) != 2 or not all(_finite(v) for v in p) for p in raw)
                        or not all(_finite(s) for s in scores)):
                    raise ValueError('Finite 133 xy coordinates and raw model confidence scores required')
                points = [list(p) if score >= threshold else None for p,score in zip(raw,scores)]
            has = lambda group: all(points[i] is not None for i in group)
            visibility = {'body':has([5,6,11,12]), 'head':has([0,1,2]),
                          'arm_left':has([5,7,9]), 'arm_right':has([6,8,10]),
                          'leg_left':has([11,13,15]), 'leg_right':has([12,14,16]),
                          'face':has(range(23,91)), 'hand_left':has(range(91,112)),
                          'hand_right':has(range(112,133)), 'feet':has(range(17,23))}
            if visibility['body']:
                shoulder = [(points[5][k]+points[6][k])/2 for k in range(2)]
                hip = [(points[11][k]+points[12][k])/2 for k in range(2)]
                length = math.dist(shoulder, hip)
                if length > 1e-6:
                    torso_lengths[role_id].append(length/1.39)
                else:
                    visibility['body'] = False
            missing = [key for key,value in visibility.items() if not value]
            uncertainty = list(role.get('uncertainty', []))
            if missing or uncertainty:
                gaps.append({'frame_index':index, 'pts_seconds':pts, 'role_id':role_id,
                             'missing_groups':missing, 'tracking_uncertainty':uncertainty,
                             'action':'hide unobserved geometry; no interpolation/hold-last-pose'})
            actors.append({'id':role_id, 'points':points, 'visibility':visibility,
                           'identity_provenance':role.get('identity_provenance'), 'uncertainty':uncertainty})
        frames.append({'index':index, 'pts_seconds':pts, 'duration_seconds':duration, 'roles':actors})
    cooked_roles = []
    for role in roles:
        if not torso_lengths[role['id']]:
            raise ValueError('No reliable torso observation to determine fixed role scale: '+role['id'])
        scale=statistics.median(torso_lengths[role['id']])
        samples={name:[] for name in ['shoulder_width','hip_width','arm_left_upper','arm_left_lower',
            'arm_right_upper','arm_right_lower','leg_left_upper','leg_left_lower','leg_right_upper','leg_right_lower']}
        chains={'arm_left':(5,7,9),'arm_right':(6,8,10),'leg_left':(11,13,15),'leg_right':(12,14,16)}
        for frame in frames:
            record=next(item for item in frame['roles'] if item['id']==role['id'])
            points=record['points']
            for name,pair in [('shoulder_width',(5,6)),('hip_width',(11,12))]:
                if all(points[i] is not None for i in pair):
                    length=math.dist(points[pair[0]],points[pair[1]])/scale
                    if length>1e-6:samples[name].append(length)
            for name,(a,b,c) in chains.items():
                if not record['visibility'][name]:continue
                upper=math.dist(points[a],points[b])/scale;lower=math.dist(points[b],points[c])/scale
                if min(upper,lower)>=.001:
                    samples[name+'_upper'].append(upper);samples[name+'_lower'].append(lower)
        if not samples['shoulder_width'] or not samples['hip_width']:
            raise ValueError('Reliable fixed projected shoulder/hip widths required for '+role['id'])
        limbs={}
        for name in chains:
            upper,lower=samples[name+'_upper'],samples[name+'_lower']
            limbs[name]={'upper':statistics.median(upper) if upper else None,
                         'lower':statistics.median(lower) if lower else None,
                         'sample_count':len(upper),'available':bool(upper and lower)}
        shape={'torso':1.39,'shoulder_width':statistics.median(samples['shoulder_width']),
               'hip_width':statistics.median(samples['hip_width']),'limbs':limbs,
               'provenance':'current-source reliable 2D segment medians; fixed screen-space staging estimate, not metric anatomy',
               'per_frame_stretch_allowed':False,'depth_or_anatomy_recovered':False,
               'minimum_mesh_segment_length':.001}
        first_frame=next(frame for frame in frames if next(item for item in frame['roles'] if item['id']==role['id'])['visibility']['body'])
        first=next(item for item in first_frame['roles'] if item['id']==role['id'])['points']
        sx,sy=((first[5][k]+first[6][k])/2 for k in range(2))
        hx,hy=((first[11][k]+first[12][k])/2 for k in range(2))
        roll=math.atan2(-(sx-hx),hy-sy)
        shoulder_x=math.cos(roll)*(first[5][0]-first[6][0])-math.sin(roll)*(first[5][1]-first[6][1])
        shoulder_span=math.dist(first[5],first[6])
        ambiguous=abs(shoulder_x)<=max(1e-6,shoulder_span*.05)
        mapping={'left_first':None if ambiguous else shoulder_x<0,'ambiguous':ambiguous,
                 'source_frame_index':first_frame['index'],'torso_local_shoulder_delta_x':shoulder_x,
                 'basis':'first reliable torso frame shoulder chord in de-rolled torso-local coordinates; no raw image-x ordering'}
        cooked_roles.append({'id':role['id'], 'animal':role['animal'],
                             'fixed_pixels_per_unit':scale,'fixed_projected_shape':shape,
                             'source_side_mapping':mapping})
    return {'schema':'animal-recreation-render-scene-v1', 'mode':MODE, 'source_sha256':source['sha256'],
            'crop_xywh':crop, 'width':crop[2], 'height':crop[3], 'roles':cooked_roles, 'frames':frames,
            'duration_seconds':native[-1]['pts_seconds']+native[-1]['duration_seconds']-native[0]['pts_seconds'],
            'observation_gaps':gaps, 'depth_recovered':False, 'contact_verified':False,
            'camera_policy':'shared orthographic image coordinates; apparent camera motion retained in observations',
            'scale_policy':'one median torso scale and one reliable projected segment-median shape per role; no per-frame stretching or metric anatomy claim',
            'missing_policy':'unobserved body/limb/head hidden; neutral unavailable expression/gesture explicitly reported'}


def _run(command, timeout=600):
    result = subprocess.run([str(v) for v in command], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{Path(str(command[0])).name} failed: {result.stderr[-5000:]}')
    return result.stdout


def _probe(path):
    return json.loads(_run(['ffprobe','-v','error','-show_streams','-show_format',
                           '-show_frames','-of','json',path]))


def _verify_video(path, expected):
    probe = _probe(path)
    frames = [f for f in probe.get('frames', []) if f.get('media_type') == 'video']
    if len(frames) != len(expected):
        raise ValueError('Encoded video frame coverage differs from source')
    origin = expected[0]['pts_seconds']
    for actual, frame in zip(frames, expected):
        if abs(float(actual['best_effort_timestamp_time'])-(frame['pts_seconds']-origin)) > .002:
            raise ValueError('Encoded video timestamp differs from native source PTS')
    stream = next(s for s in probe['streams'] if s['codec_type']=='video')
    duration = expected[-1]['pts_seconds']+expected[-1]['duration_seconds']-origin
    if abs(float(stream.get('duration', probe['format']['duration']))-duration) > .003:
        raise ValueError('Encoded video duration differs from native source coverage')
    return {'frame_count':len(frames), 'duration_seconds':duration,
            'width':stream['width'], 'height':stream['height'], 'native_pts_verified':True}


def _write_json(path, data):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)


def _current_inputs(project_dir, observations_path, *, binding=None, digest=None):
    """Apply the project-store boundary equally to direct API and CLI callers."""
    try:
        from . import project_store as store
    except ImportError:
        import project_store as store
    project = store.load_project(project_dir)
    artifact = store.get_artifact(project_dir, 'observations')
    tracks = store.get_artifact(project_dir, 'tracks')
    if (Path(artifact['path']).resolve(strict=True) != observations_path
            or sha256(observations_path) != artifact['sha256']):
        raise ValueError('Renderer requires the registered current observations path and SHA256')
    if (artifact['binding_sha256'] != project['binding_sha256']
            or artifact.get('depends_on', {}).get('tracks') != tracks['sha256']):
        raise ValueError('Current observations must be bound to current roles and identity tracks')
    if binding is not None and project['binding_sha256'] != binding:
        raise ValueError('Project roles changed during rendering; output is stale')
    if digest is not None and artifact['sha256'] != digest:
        raise ValueError('Current observations changed during rendering; output is stale')
    return project, artifact


def render_preview(project_dir, observations_path, output_dir, *, settings=None):
    """Create a new review version under a project; return bound local artifacts."""
    settings = dict(settings or {})
    project_dir = Path(project_dir).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    if not output_dir.is_relative_to(project_dir) or output_dir == project_dir:
        raise ValueError('Preview output must remain under the project directory')
    if output_dir.exists():
        raise ValueError('Refusing to overwrite existing output; choose a new version directory')
    observations_path = Path(observations_path).resolve(strict=True)
    project, observation_artifact = _current_inputs(project_dir, observations_path)
    observation_hash = observation_artifact['sha256']
    observations = json.loads(observations_path.read_text(encoding='utf-8'))
    scene = prepare_scene(project, observations, settings)
    resources = resource_report(settings)
    if not resources['ready']:
        raise ValueError('Render resources unavailable: '+', '.join(resources['missing']))
    width = settings.get('width', 960)
    if type(width) is not int or width < 128 or width > 3840 or width % 2:
        raise ValueError('width must be an even integer in [128,3840]')
    timeout = settings.get('timeout_seconds', max(180, len(scene['frames'])*10))
    if not _finite(timeout) or timeout < 30:
        raise ValueError('timeout_seconds must be a finite number of at least 30')
    source = project['source']
    actual = _probe(source['path'])
    video = next(s for s in actual['streams'] if s['codec_type']=='video')
    actual_frames = [f for f in actual.get('frames',[]) if f.get('media_type')=='video'
                     and f.get('stream_index') == video['index']]
    if len(actual_frames)!=len(scene['frames']) or (video['width'],video['height']) != (source['width'],source['height']):
        raise ValueError('Decoded source/frame dimensions or coverage differ from project')
    for got,want in zip(actual_frames,scene['frames']):
        if abs(float(got['best_effort_timestamp_time'])-want['pts_seconds']) > .002:
            raise ValueError('Decoded source timestamps differ from project frame manifest')
        actual_duration = got.get('duration_time',got.get('pkt_duration_time'))
        if actual_duration is not None and abs(float(actual_duration)-want['duration_seconds']) > .002:
            raise ValueError('Decoded source frame duration differs from project manifest')
    if actual_frames[-1].get('duration_time',actual_frames[-1].get('pkt_duration_time')) is None:
        raise ValueError('Last decoded source frame needs an actual duration; no padding estimate allowed')
    if any(s.get('side_data_list') and any(abs(d.get('rotation',0)) > .01 for d in s['side_data_list']) for s in [video]):
        raise ValueError('Rotated source requires explicit normalized project initialization')
    audio_count = sum(s['codec_type']=='audio' for s in actual['streams'])
    if audio_count != source['audio_stream_count']:
        raise ValueError('Decoded audio streams differ from project')
    # Atomic creation refuses a concurrent writer and symlink-resolved escapes.
    output_dir.mkdir(parents=True, exist_ok=False)
    scene['render_width'] = width
    scene_path = output_dir/'scene.json'
    _write_json(scene_path, scene)
    command = ['node',HERE/'render_scene.mjs','--scene',scene_path,'--assets',resources['assets_dir'],
               '--output',output_dir,'--browser',resources['browser_path'],
               '--timeout-ms',round((timeout-10)*1000)]
    try:
        _run(command, timeout=timeout)
        facts = json.loads((output_dir/'render-facts.json').read_text(encoding='utf-8'))
        n = len(scene['frames'])
        duration = scene['duration_seconds']
        # Per-file high-rate input avoids concat's default 25fps PTS quantization.
        concat = ['ffconcat version 1.0']
        for index, frame in enumerate(scene['frames']):
            frame_duration = (scene['frames'][index+1]['pts_seconds']-frame['pts_seconds']
                              if index+1 < n else frame['duration_seconds'])
            concat += [f"file 'frames/{frame['index']:06d}.png'", 'option framerate 1000000',
                       f"duration {frame_duration:.9f}"]
        concat += [f"file 'frames/{n-1:06d}.png'", 'option framerate 1000000']
        concat_path = output_dir/'frames.ffconcat'
        concat_path.write_text('\n'.join(concat)+'\n', encoding='utf-8')
        silent = output_dir/'cg-silent.mp4'
        # setts gives the final real frame its measured packet duration. This
        # avoids appending a duplicate frame or assigning a synthetic CFR rate.
        # -bf 0 keeps packet N aligned with presentation-order frame indexes.
        last_duration_us = round(scene['frames'][-1]['duration_seconds']*1000000)
        duration_bsf = f'setts=duration=if(eq(N\\,{n-1})\\,{last_duration_us}\\,DURATION)'
        _run(['ffmpeg','-v','error','-n','-f','concat','-safe','0','-i',concat_path,
              '-an','-c:v','libx264','-bf','0','-preset','fast','-crf','18','-pix_fmt','yuv420p',
              '-fps_mode','passthrough','-enc_time_base','1:1000000','-bsf:v',duration_bsf,
              '-frames:v',n,'-video_track_timescale','1000000','-movflags','+faststart',silent],timeout)
        video_facts = _verify_video(silent, scene['frames'])
        audio = output_dir/'cg-original-audio.mp4'
        comparison = output_dir/'comparison.mp4'
        # -copyts plus a common origin preserves the audio/video source offset.
        # No shortest/loop/padding flags; video coverage is verified afterwards.
        origin = scene['frames'][0]['pts_seconds']
        audio_args = []
        if audio_count:
            audio_args = ['-map','1:a?', '-af',f'asetpts=PTS-({origin})/TB,aresample=async=0:first_pts=0', '-c:a','aac','-b:a','192k']
        _run(['ffmpeg','-v','error','-n','-copyts','-i',silent,'-i',source['path'],
              '-map','0:v:0','-c:v','copy',*audio_args,'-t',f'{duration:.9f}',
              '-video_track_timescale','1000000','-movflags','+faststart',audio],timeout)
        view = facts['viewport']
        x,y,w,h = source['crop_xywh']
        padw,padh = view['width'],view['height']
        px,py = -view['x'], -view['y']
        outw,outh = facts['width'],facts['height']
        graph = (f'[0:v]format=rgb24,crop={w}:{h}:{x}:{y}:exact=1,pad={padw}:{padh}:{px}:{py}:color=0x111923,'
                 f'scale={outw}:{outh}:flags=lanczos,setsar=1,setpts=PTS-({origin})/TB[src];'
                 f'[1:v]setsar=1[cg];[src][cg]hstack=inputs=2:shortest=1[v]')
        map_audio = ['-map','0:a?', '-af',f'asetpts=PTS-({origin})/TB,aresample=async=0:first_pts=0','-c:a','aac','-b:a','192k'] if audio_count else []
        _run(['ffmpeg','-v','error','-n','-copyts','-noautorotate','-i',source['path'],'-i',silent,
              '-filter_complex',graph,'-map','[v]',*map_audio,'-c:v','libx264','-bf','0','-preset','fast',
              '-crf','19','-pix_fmt','yuv420p','-fps_mode','passthrough','-enc_time_base','1:1000000',
              '-bsf:v',duration_bsf,'-frames:v',n,'-t',f'{duration:.9f}',
              '-video_track_timescale','1000000','-movflags','+faststart',comparison],timeout)
        _verify_video(audio, scene['frames'])
        _verify_video(comparison, scene['frames'])
        if sha256(source['path']) != source['sha256'] or sha256(observations_path) != observation_hash:
            raise ValueError('Source/observations changed during rendering; preview invalid')
        _current_inputs(project_dir,observations_path,binding=project['binding_sha256'],digest=observation_hash)
        review = output_dir/'review.html'
        artifacts = {'cg_silent':str(silent), 'cg_audio':str(audio),
                     'comparison':str(comparison), 'review':str(review)}
        report = {'schema':'animal-recreation-preview-report-v1', 'mode':MODE,
                  'project_id':project['project_id'], 'source_sha256':source['sha256'],
                  'binding_sha256':project['binding_sha256'],
                  'observations_sha256':observation_hash, 'artifacts':artifacts,
                  'resources':resources, 'video':video_facts, 'render':facts,
                  'observation_gaps':scene['observation_gaps'], 'audio_stream_count':audio_count,
                  'audio_status':'original local audio transcoded with source timing; initial source gaps represented by silence, samples outside video coverage trimmed' if audio_count else 'source has no audio; cg_audio is a silent local review copy',
                  'paid_provider_calls':0, 'source_uploads':0, 'status':'needs_review',
                  'depth_recovered':False, 'contact_verified':False, 'visual_fidelity_accepted':False,
                  'limitations':['Screen-space 2.5D animation reference; no calibrated camera, metric depth, ground or contact recovery.',
                      'One fixed projected segment-median shape per role is estimated from current reliable 2D observations, not metric anatomy; bounded IK residuals and unavailable branches are reported.',
                      '2D face/hand cues are canine analogues, not recovered emotion, human fingers or hidden motion.',
                      'Camera/image coordinates are shared; image motion includes camera motion rather than separating physical camera and actor motion.']}
        review.write_text(_review_html(report), encoding='utf-8')
        report['artifact_sha256'] = {key:sha256(path) for key,path in artifacts.items()}
        _write_json(output_dir/'report.json',report)
        return report
    except Exception as exc:
        _write_json(output_dir/'failure.json',{'status':'failed','error':str(exc),
                    'source_sha256':source['sha256'], 'observations_sha256':observation_hash})
        raise


def _review_html(report):
    limitations = ''.join('<li>'+html.escape(v)+'</li>' for v in [
        '当前仅为屏幕空间 2.5D 动作参考，真实相机、深度、地面和物理接触尚未恢复。',
        '每个角色从本片可靠二维观测估计一次固定比例，肢段不逐帧拉长；有界 IK 仍无法达到的目标、缺测和不可解分支会公开记录。',
        '手脸只使用已实现的二维提示映射为犬体动作，不代表完整手指、真实情绪或隐藏动作已恢复。',
        '角色与原片共用图像坐标；画面中的镜头运动与角色运动尚未分离。'])
    details = html.escape(json.dumps({'source_sha256':report['source_sha256'],
              'observations_sha256':report['observations_sha256'],
              'video':report['video'], 'render':report['render'],
              'observation_gaps':report['observation_gaps']},ensure_ascii=False,indent=2))
    audio_note=('已保留原片声音与源时间关系，原片开头的音频空段按原时钟保留，超出视频覆盖的声音已截取。'
                if report['audio_stream_count'] else '原片没有音轨，原音版链接提供静音审阅副本。')
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>双犬 CG 同步审阅</title>
<style>body{{font:16px system-ui;background:#111923;color:#e8eef6;margin:24px;max-width:1400px}}video{{width:100%;max-height:75vh;background:#000}}summary{{cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.notice{{color:#ffca87}}</style>
<h1>屏幕空间 2.5D 双犬 CG 动作参考</h1><p class="notice">待人工审阅：动作效果、深度、地面与接触均未验收。</p>
<p>左侧：原片裁切；右侧：犬体 CG。两侧使用同一图像视口和原生帧时间，便于同步核对。</p>
<video src="comparison.mp4" controls preload="metadata"></video>
<p><a href="cg-silent.mp4">干净静音 CG</a> · <a href="cg-original-audio.mp4">CG 原音版</a> · <a href="report.json">完整校验报告</a></p>
<p>{audio_note}源观测缺测或不确定记录共 {len(report['observation_gaps'])} 条，未观测到的部位不会沿用上一帧或补造动作；其他不可解分支见报告。</p>
<p>请先原速查看完整对照，检查角色、动作节奏、肘膝与手脚、头颈连接和构图。需要修正时请指出秒点、角色和现象；明确接受这版 CG 后才记录人工验收。接受 CG 本身不代表授权付费生成。</p>
<ul>{limitations}</ul><details><summary>时间、固定比例、实际网格和缺测证据（技术 JSON）</summary><pre>{details}</pre></details>
</html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--observations',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--browser',type=Path)
    parser.add_argument('--width',type=int,default=960)
    args = parser.parse_args()
    settings = {'width':args.width}
    if args.browser:
        settings['browser_path'] = str(args.browser)
    result = render_preview(args.project,args.observations,args.output,settings=settings)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
