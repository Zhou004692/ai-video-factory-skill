"""Pure local packaging of a native cloud result. No network or credential imports."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import subprocess
import uuid


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command):
    result = subprocess.run(list(map(str, command)), capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=600)
    if result.returncode:
        raise ValueError('Local media packaging failed; native original remains unchanged')


def _timeline(probe):
    frames = probe.get('frames') or []
    if not frames or len(frames) != probe['video']['frame_count']:
        raise ValueError('Native video needs actual decoded frame timestamps before packaging')
    timestamps = [frame.get('pts_seconds') for frame in frames]
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in timestamps):
        raise ValueError('Finite native frame PTS are required')
    if any(b <= a for a,b in zip(timestamps, timestamps[1:])):
        raise ValueError('Native frame PTS must be strictly increasing')
    last = frames[-1].get('duration_seconds')
    if type(last) not in (int,float) or not math.isfinite(last) or last <= 0:
        raise ValueError('Measured last native frame duration is required; no estimated tail padding')
    return timestamps[0], timestamps[-1] + last - timestamps[0]


def _delivery_dir(project_dir, run_dir):
    project = Path(project_dir).resolve(strict=True)
    run = Path(run_dir).resolve(strict=True)
    if project not in run.parents:
        raise ValueError('Generation run must remain under the project')
    folder = run/'delivery'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8])
    resolved = folder.resolve()
    # Resolve intermediate directory junctions before creating anything. Reject
    # even an internal redirect so the publication path is unambiguous.
    if resolved != folder or run not in resolved.parents or project not in resolved.parents:
        raise ValueError('Delivery path escapes the generation run/project through a junction or symlink')
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def package_native(project_dir, run_dir, plan, native, native_probe):
    """Keep all native frames, replace audio locally, and compare only real common coverage."""
    try:
        from . import project_store as store
        from .generation import probe_media
    except ImportError:
        import project_store as store
        from generation import probe_media
    project = store.load_project(project_dir)
    source = project['source']
    if source['sha256'] != plan['cg']['source_sha256']:
        raise ValueError('Original source does not match the purchased CG binding')
    native = Path(native).resolve(strict=True)
    run_dir = Path(run_dir).resolve(strict=True)
    if native.parent != run_dir or _sha(source['path']) != source['sha256']:
        raise ValueError('Source/native path or bytes changed')
    native_sha = _sha(native)
    origin, duration = _timeline(native_probe)
    source_origin = source['frames'][0]['pts_seconds']
    source_duration = source['frames'][-1]['pts_seconds'] + source['frames'][-1]['duration_seconds'] - source_origin
    common = min(duration, source_duration)
    if common <= 0:
        raise ValueError('No common real video window exists')
    folder = _delivery_dir(project_dir, run_dir)
    audio = folder/'model-original-audio.mp4'
    comparison = folder/'comparison.mp4'
    review = folder/'review.html'
    audio_map = []
    if source['audio_stream_count']:
        audio_map = ['-map','1:a?', '-af',
            f'asetpts=PTS-({source_origin})/TB,atrim=start=0:end={duration:.9f}',
            '-c:a','aac','-b:a','192k']
    # Video is copied, with its real origin normalized. Audio may end earlier;
    # no shortest, loop, apad, fps or video-duration fabrication is used.
    _run(['ffmpeg','-v','error','-n','-copyts','-itsoffset',f'{-origin:.9f}','-i',native,
          '-i',source['path'],'-map','0:v:0','-c:v','copy',*audio_map,'-t',f'{duration:.9f}',
          '-video_track_timescale','1000000','-movflags','+faststart',audio])
    audio_probe = probe_media(audio)
    audio_origin, audio_duration = _timeline(audio_probe)
    if (audio_probe['video']['frame_count'] != native_probe['video']['frame_count']
            or abs(audio_duration-duration) > .003
            or any(abs((a['pts_seconds']-audio_origin)-(b['pts_seconds']-origin)) > .003
                   for a,b in zip(audio_probe['frames'], native_probe['frames']))):
        raise ValueError('Source-audio package changed native video coverage; original preserved')
    x,y,w,h = source['crop_xywh']
    height = min(720, native_probe['video']['height'])//2*2
    native_width = round(native_probe['video']['width']/native_probe['video']['height']*height/2)*2
    source_width = round(w/h*height/2)*2
    graph = (f'[0:v]setpts=PTS-({origin})/TB,scale={native_width}:{height},setsar=1,'
             f'pad={native_width+source_width}:{height}:0:0:color=black[base];'
             f'[1:v]crop={w}:{h}:{x}:{y}:exact=1,setpts=PTS-({source_origin})/TB,'
             f'scale={source_width}:{height},setsar=1[src];'
             # A decoded source EOF may occur at its final frame PTS, before
             # that frame's measured display duration has elapsed. Sampling its
             # last image is valid until source_origin+source_duration only.
             # The explicit common bound below enforces that end, while the
             # primary native stream supplies every output frame timestamp.
             f'[base][src]overlay=x={native_width}:y=0:shortest=0:eof_action=repeat:repeatlast=1[v]')
    comparison_audio = []
    if source['audio_stream_count']:
        comparison_audio = ['-map','1:a?', '-af',
            f'asetpts=PTS-({source_origin})/TB,atrim=start=0:end={common:.9f}',
            '-c:a','aac','-b:a','192k']
    expected = [f['pts_seconds']-origin for f in native_probe['frames'] if f['pts_seconds']-origin < common-.000001]
    last_duration_us = round((common-expected[-1])*1000000)
    duration_bsf = f'setts=duration=if(eq(N\\,{len(expected)-1})\\,{last_duration_us}\\,DURATION)'
    _run(['ffmpeg','-v','error','-n','-copyts','-i',native,'-noautorotate','-i',source['path'],
          '-filter_complex',graph,'-map','[v]',*comparison_audio,'-c:v','libx264','-bf','0',
          '-preset','fast','-crf','19','-pix_fmt','yuv420p','-fps_mode','passthrough',
          '-enc_time_base','1:1000000','-video_track_timescale','1000000',
          '-bsf:v',duration_bsf,'-frames:v',len(expected),'-t',f'{common:.9f}',
          '-movflags','+faststart',comparison])
    comparison_probe = probe_media(comparison)
    comparison_origin, comparison_duration = _timeline(comparison_probe)
    actual = [f['pts_seconds']-comparison_origin for f in comparison_probe['frames']]
    if len(actual) != len(expected) or any(abs(a-b) > .003 for a,b in zip(actual,expected)):
        raise ValueError(f'Comparison did not retain the native frame clock (expected {len(expected)}, got {len(actual)}); '
                         'inspect local candidate, never resubmit')
    if abs(comparison_duration-common) > .003:
        raise ValueError('Comparison escaped its real common display window; original preserved')
    if _sha(native) != native_sha or _sha(source['path']) != source['sha256']:
        raise ValueError('Native/source bytes changed during packaging')
    report = {'schema':'recreation-generation-delivery-v1','status':'local_delivery_ready_for_review',
        'native':{'path':str(native),'sha256':native_sha,'probe':native_probe},
        'source_sha256':source['sha256'],'native_video_coverage_seconds':duration,
        'source_video_coverage_seconds':source_duration,'comparison_common_window_seconds':common,
        'comparison_encoded_duration_seconds':comparison_duration,
        'native_frames':native_probe['video']['frame_count'],'duration_padding_applied':False,
        'source_audio_status':'local source audio trimmed/aligned; no video padding' if source['audio_stream_count']
                              else 'source has no audio; local package remains silent',
        'artifacts':{'original_audio':str(audio),'comparison':str(comparison),'review':str(review)},
        'original_audio_probe':audio_probe,'comparison_probe':comparison_probe,
        'visual_quality_accepted':False,'source_uploads':0,'paid_provider_calls':0}
    summary = {k:report[k] for k in ('native_video_coverage_seconds','source_video_coverage_seconds',
        'comparison_common_window_seconds','native_frames','duration_padding_applied','source_audio_status')}
    text = html.escape(json.dumps(summary,ensure_ascii=False,indent=2))
    review.write_text(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>生成结果本地审阅</title>
<style>body{{font:16px/1.6 system-ui;max-width:1400px;margin:24px auto;background:#f4f6f9;color:#233047}}video{{width:100%;max-height:75vh}}pre{{white-space:pre-wrap}}</style>
<h1>生成结果本地审阅</h1><p>对比：左为模型原生画面，右为原片裁切；仅展示实际共同时间窗。请原速检查完整事件、身份、动作、构图和自然度。</p>
<video controls src="comparison.mp4"></video><h2>完整原生画面＋本地原音</h2>
<p>保留全部模型视频帧；原音仅在本地对齐，可能早于视频结束。此包装不证明动作或情绪已通过验收。</p>
<video controls src="model-original-audio.mp4"></video><pre>{text}</pre></html>''',encoding='utf-8')
    report['artifact_sha256'] = {k:_sha(v) for k,v in report['artifacts'].items()}
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
