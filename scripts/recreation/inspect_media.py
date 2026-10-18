"""Read local source facts with Python standard library and ffprobe only."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

def inspect_media(source: Path) -> dict:
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError('Source must be a local media file')
    digest = hashlib.sha256()
    with source.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    r = subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json', str(source)], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    if r.returncode:
        raise ValueError('ffprobe could not decode source metadata')
    raw = json.loads(r.stdout)
    video = next((s for s in raw.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not video:
        raise ValueError('No video stream found')
    count = video.get('nb_read_frames')
    return {'source': str(source), 'source_sha256': digest.hexdigest(), 'source_bytes': source.stat().st_size,
            'video': {'width': video['width'], 'height': video['height'], 'frame_count': int(count) if str(count).isdigit() else None,
                      'avg_frame_rate': video.get('avg_frame_rate'), 'r_frame_rate': video.get('r_frame_rate'),
                      'time_base': video.get('time_base'), 'duration': video.get('duration') or raw.get('format', {}).get('duration'),
                      'rotation_metadata': video.get('side_data_list', [])},
            'audio_stream_count': sum(s.get('codec_type') == 'audio' for s in raw.get('streams', [])),
            'crop_not_selected': True, 'timestamp_mapping_not_verified': True,
            'full_recreation_accepted': False, 'paid_provider_calls': 0, 'source_uploads': 0}

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__);p.add_argument('source', type=Path);args = p.parse_args()
    try:
        print(json.dumps(inspect_media(args.source), ensure_ascii=False, indent=2))
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        p.exit(1, f'Local media inspection failed: {exc}\n')
