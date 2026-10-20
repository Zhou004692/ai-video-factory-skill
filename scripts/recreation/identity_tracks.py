"""Headless CPU identity evidence from explicit anchors and sparse LK flow.

No detector is an identity oracle. HOG proposals are optional human selection
aids only. Tracking stays missing after ambiguity until another explicit anchor.
All propagated boxes require review; this does not infer animal identity or 3D.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from project_store import load_project, record_artifact, file_sha256, _internal, _root, _validate_roles

def _missing(role_id, reason, provenance=None):
    return {'id': role_id, 'bbox_xyxy': None, 'identity_provenance': provenance,
        'uncertainty': [reason], 'needs_review': True, 'missing_observation': True}

def _features(gray, box):
    mask = np.zeros_like(gray)
    x, y, x2, y2 = box
    # Avoid selecting fixed background immediately outside the reviewed subject.
    mask[int(np.ceil(y)):int(y2)+1, int(np.ceil(x)):int(x2)+1] = 255
    return cv2.goodFeaturesToTrack(gray, maxCorners=120, qualityLevel=.02,
        minDistance=4, mask=mask, blockSize=5)

def _propagate(previous, current, state):
    box, points, provenance = state
    if points is None or len(points) < 6: return None, 'insufficient_texture'
    settings = {'winSize': (21, 21), 'maxLevel': 3,
        'criteria': (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, .01)}
    forward, status, error = cv2.calcOpticalFlowPyrLK(previous, current, points, None, **settings)
    if forward is None: return None, 'flow_unavailable'
    backward, reverse_status, _ = cv2.calcOpticalFlowPyrLK(current, previous, forward, None, **settings)
    if backward is None: return None, 'flow_unavailable'
    p, q, back = points.reshape(-1, 2), forward.reshape(-1, 2), backward.reshape(-1, 2)
    fb = np.linalg.norm(p-back, axis=1)
    valid = ((status.ravel() == 1) & (reverse_status.ravel() == 1) &
        np.isfinite(q).all(axis=1) & np.isfinite(back).all(axis=1) & (fb <= 1.) & (error.ravel() <= 20.))
    if valid.sum() < 6 or valid.mean() < .6: return None, 'occluded_or_insufficient_flow_support'
    delta = q[valid]-p[valid]; movement = np.median(delta, axis=0)
    residual = np.linalg.norm(delta-movement, axis=1)
    consistent = residual <= 2.5
    if consistent.sum() < 6 or consistent.mean() < .7: return None, 'nonrigid_or_ambiguous_flow'
    dx, dy = map(float, movement)
    result = [box[0]+dx, box[1]+dy, box[2]+dx, box[3]+dy]
    h, w = current.shape
    if (result[0] < 0 or result[1] < 0 or result[2] >= w or result[3] >= h or
        np.linalg.norm(movement) > min(box[2]-box[0]+1, box[3]-box[1]+1)*.6):
        return None, 'out_of_bounds_or_large_motion'
    evidence = {'method': 'anchored_sparse_lk_forward_backward',
        'anchor_frame_index': provenance['anchor_frame_index'],
        'anchor_provenance': provenance.get('anchor_provenance', 'user_supplied_estimate'),
        'forward_backward_median_px': float(np.median(fb[valid])),
        'supported_features': int(consistent.sum()), 'support_fraction': float(valid.mean()),
        'translation_only': True}
    # Retain only supported original subject features; never reseed background.
    new_points = q[valid][consistent].reshape(-1, 1, 2).astype(np.float32)
    return (result, new_points, evidence), None

def _overlap(a, b):
    x = max(0., min(a[2], b[2])-max(a[0], b[0])+1)
    y = max(0., min(a[3], b[3])-max(a[1], b[1])+1)
    return x*y / min((a[2]-a[0]+1)*(a[3]-a[1]+1), (b[2]-b[0]+1)*(b[3]-b[1]+1))

def track_frames(frames, roles):
    """Stream crop BGR frames to complete vitpose-compatible identity rows.

    Anchors correct their exact frame; frames before the first anchor are
    deliberately missing. A flow failure is not automatically reacquired.
    """
    roles = json.loads(json.dumps(roles))
    if not roles or len({r['id'] for r in roles}) != len(roles): raise ValueError('Unique explicit role identities required')
    for role in roles:
        indices = [a['frame_index'] for a in role['anchors']]
        if len(set(indices)) != len(indices) or any(type(n) is not int or n < 0 for n in indices):
            raise ValueError('Unique nonnegative anchor frame indices required')
    anchors = {r['id']: {a['frame_index']: a for a in r['anchors']} for r in roles}
    states = {}; reasons = {}; rows = []; previous = None; shape = None
    for index, frame in enumerate(frames):
        if frame is None or frame.ndim != 3 or frame.shape[2] != 3: raise ValueError('BGR crop frames required')
        if shape is None: shape = frame.shape
        if frame.shape != shape: raise ValueError('Frame size changed')
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY); records = []
        for role in roles:
            role_id = role['id']; anchor = anchors[role_id].get(index)
            if anchor is not None:
                box = list(map(float, anchor['bbox_xyxy']))
                if len(box) != 4 or not np.isfinite(box).all() or not 0 <= box[0] <= box[2] < shape[1] or not 0 <= box[1] <= box[3] < shape[0]:
                    raise ValueError('Anchor outside decoded crop')
                reviewed = anchor.get('provenance') == 'user_reviewed'
                provenance = {'method': 'explicit_anchor', 'anchor_frame_index': index,
                    'anchor_provenance': anchor.get('provenance', 'user_supplied_estimate')}
                states[role_id] = (box, _features(gray, box), provenance)
                records.append({'id': role_id, 'bbox_xyxy': box, 'identity_provenance': provenance,
                    'uncertainty': [] if reviewed else ['anchor_requires_review'], 'needs_review': not reviewed})
            elif states.get(role_id) is not None:
                state, reason = _propagate(previous, gray, states[role_id]); states[role_id] = state
                if state is None:
                    reasons[role_id] = reason; records.append(_missing(role_id, reason))
                else:
                    records.append({'id': role_id, 'bbox_xyxy': state[0], 'identity_provenance': state[2],
                        'uncertainty': ['propagated_identity_requires_review', 'translation_only_box'], 'needs_review': True})
            else:
                records.append(_missing(role_id, reasons.get(role_id, 'before_first_anchor')))
        ambiguous = set()
        for i, first in enumerate(records):
            for second in records[i+1:]:
                if first['bbox_xyxy'] is not None and second['bbox_xyxy'] is not None and _overlap(first['bbox_xyxy'], second['bbox_xyxy']) > .2:
                    ambiguous.update((first['id'], second['id']))
        for i, record in enumerate(records):
            if record['id'] in ambiguous and index not in anchors[record['id']]:
                role_id = record['id']; states[role_id] = None; reasons[role_id] = 'identity_overlap_ambiguous'
                records[i] = _missing(role_id, reasons[role_id], record['identity_provenance'])
        rows.append({'source_frame_index': index, 'roles': records}); previous = gray
    if not rows: raise ValueError('No decoded source frames')
    for role in roles:
        if any(type(a['frame_index']) is not int or not 0 <= a['frame_index'] < len(rows) for a in role['anchors']):
            raise ValueError('Anchor is not a decoded frame index')
    return rows

def _crop_frames(source):
    capture = cv2.VideoCapture(source['path'])
    if not capture.isOpened(): raise ValueError('OpenCV could not open source')
    x, y, w, h = source['crop_xywh']
    try:
        while True:
            ok, frame = capture.read()
            if not ok: break
            if frame.shape[:2] != (source['height'], source['width']): raise ValueError('Decoded dimensions differ from manifest')
            yield frame[y:y+h, x:x+w]
    finally: capture.release()

def run(project_dir, output):
    project = load_project(project_dir); source = project['source']; root = _root(project_dir)
    output = _internal(root, output, exists=False)
    if output.exists(): raise ValueError('Refusing to overwrite tracks; choose a new output')
    _validate_roles(project['roles'], source)
    rows = track_frames(_crop_frames(source), project['roles'])
    if len(rows) != source['frame_count']: raise ValueError('Decoded frame coverage differs from manifest')
    if file_sha256(source['path']) != source['sha256']: raise ValueError('Source changed during tracking')
    for row, manifest in zip(rows, source['frames']):
        row['timestamp_seconds'] = manifest['pts_seconds']; row['duration_seconds'] = manifest['duration_seconds']
    report = {'schema_version': 1, 'source_sha256': source['sha256'], 'source_path': source['path'],
        'binding_sha256': project['binding_sha256'], 'fps': source['fps'], 'crop_xywh': source['crop_xywh'],
        'rows': rows, 'identity_accepted': False, 'source_uploads': 0, 'paid_provider_calls': 0,
        'method': 'explicit anchors + CPU sparse LK forward/backward checks; no automatic identity reacquisition'}
    # Recheck state after long decode, before publishing or accepting a binding.
    if load_project(project_dir)['binding_sha256'] != project['binding_sha256']: raise ValueError('Roles changed during tracking')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream: json.dump(report, stream, ensure_ascii=False, allow_nan=False)
    record_artifact(project_dir, 'tracks', output)
    return report

def propose_candidates(image_bgr):
    """OpenCV built-in human HOG suggestions only; no identity assignment."""
    if image_bgr.shape[0] < 128 or image_bgr.shape[1] < 64: return []
    hog = cv2.HOGDescriptor(); hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    boxes, weights = hog.detectMultiScale(image_bgr, winStride=(8, 8), padding=(8, 8), scale=1.05)
    return [{'bbox_xyxy': [int(x), int(y), int(x+w-1), int(y+h-1)], 'score': float(score),
        'needs_review': True, 'identity_assigned': False, 'detector': 'opencv_builtin_human_hog'}
        for (x, y, w, h), score in zip(boxes, weights)]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.project, args.output)
        print(json.dumps({'output': str(args.output), 'frames': len(report['rows']), 'needs_review': True}))
    except (ValueError, OSError, KeyError) as exc: parser.exit(1, str(exc)+'\n')

if __name__ == '__main__': main()
