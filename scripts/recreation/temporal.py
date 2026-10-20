"""Auditable render guides; never replace raw observations or certify motion.

One Euro equations: Casiez et al., https://gery.casiez.net/1euro/ . Unlike the
legacy demo's frame-count clock, this adapter uses actual seconds. Visibility,
resolution and a caller-declared rendering speed bound are separate policies.
"""
from __future__ import annotations
import math


def content_time_map(source_frames,source_timestamps_seconds,reviewed_pairs,*,pinned_source_frames=()):
    """Explicit shared render-state clock for reviewed repeated pictures.

    Preserve every source frame and original presentation timestamp. The map
    must be used for camera AND all actor/scene states, not only one skeleton.
    Low image difference alone does not authorize freezing a performance.
    Raw pose observations remain per original frame, separate from this map.
    """
    ids=list(source_frames);times=list(source_timestamps_seconds)
    if not ids or len(ids)!=len(times) or any(type(n)!=int or n<0 for n in ids) or any(b<=a for a,b in zip(ids,ids[1:])):raise ValueError('Aligned strictly increasing integer source frames required')
    if any(not isinstance(t,(int,float)) or not math.isfinite(t) for t in times) or any(b<=a for a,b in zip(times,times[1:])):raise ValueError('Strictly increasing finite source presentation timestamps required')
    pin=list(pinned_source_frames)
    if any(type(n)!=int or n not in ids for n in pin) or len(pin)!=len(set(pin)):raise ValueError('Unique pinned source frames inside the mapping required')
    index={n:k for k,n in enumerate(ids)};equal_edges=set();seen=set();evidence=[]
    for row in reviewed_pairs:
        if not isinstance(row,dict) or type(row.get('same_content'))!=bool or not isinstance(row.get('evidence_id'),str) or not row['evidence_id'].strip():raise ValueError('Explicit boolean review and nonempty evidence_id required')
        pair=row.get('source_frames')
        if not isinstance(pair,(list,tuple)) or len(pair)!=2 or any(type(n)!=int or n not in index for n in pair):raise ValueError('Explicit adjacent source pair inside the mapping required')
        a,b=pair
        if b!=a+1 or index[b]!=index[a]+1 or (a,b) in seen:raise ValueError('Unique adjacent ordered source pairs required')
        seen.add((a,b));evidence.append(dict(source_frames=[a,b],same_content=row['same_content'],evidence_id=row['evidence_id']))
        if row['same_content']:equal_edges.add((a,b))
    groups=[]
    for n in ids:
        if groups and (groups[-1][-1],n) in equal_edges:groups[-1].append(n)
        else:groups.append([n])
    representatives={}
    for group in groups:
        anchors=[n for n in group if n in pin]
        if len(anchors)>1:raise ValueError('Multiple pinned camera/pose anchors in one content group require upstream reconciliation')
        leader=anchors[0] if anchors else group[0]
        for n in group:representatives[n]=leader
    return dict(source_frames=ids,source_timestamps_seconds=times,representative_source_frames=[representatives[n] for n in ids],group_source_frames=groups,
                reviewed_pair_evidence=evidence,pinned_source_frames=pin,shared_camera_and_all_actor_clock_required=True,
                source_capture_time_recovered=False,raw_observations_changed=False,automatically_accepted=False)


def resolved_axis(vector, score, *, grid_px, minimum_score=.35):
    """Require a two-cell lever arm before estimating a small part's angle.

    This conservative resolution policy is NOT a confidence calibration.
    Grid size is the source-space heatmap cell, not the source pixel size.
    """
    if (len(vector)!=2 or not all(math.isfinite(v) for v in vector)
            or not math.isfinite(score) or not math.isfinite(grid_px) or grid_px<=0):
        raise ValueError('Finite 2D vector, score and positive heatmap grid required')
    if score<minimum_score or math.hypot(*vector)<2*grid_px:return None
    return math.atan2(vector[1],vector[0])


class AngleGuide:
    """Causal One Euro angle + explicit missing/reacquisition state.

    Hold on unknown input; never silently assert zero or extrapolate forever.
    Speed limiting is an authored safety bound, not inferred biomechanics.
    """
    def __init__(self, *, max_speed, min_cutoff=1.7, beta=.3, derivative_cutoff=1., initial_angle=None):
        values=(max_speed,min_cutoff,beta,derivative_cutoff)
        if not all(math.isfinite(v) for v in values) or min(max_speed,min_cutoff,derivative_cutoff)<=0 or beta<0:
            raise ValueError('Finite positive speed/cutoffs and nonnegative beta required')
        self.max_speed=max_speed;self.min_cutoff=min_cutoff;self.beta=beta;self.dc=derivative_cutoff
        if initial_angle is not None and not math.isfinite(initial_angle):raise ValueError('Finite explicit render prior required')
        self.time=None;self.angle=initial_angle;self.raw=initial_angle;self.derivative=0.;self.missing=True

    @staticmethod
    def alpha(dt,cutoff):
        x=2*math.pi*cutoff*dt
        return x/(1+x)

    def update(self,timestamp,angle):
        if not math.isfinite(timestamp) or (self.time is not None and timestamp<=self.time):
            raise ValueError('Strictly increasing finite timestamps required')
        if angle is not None and not math.isfinite(angle):raise ValueError('Finite angle or explicit None required')
        dt=None if self.time is None else timestamp-self.time;self.time=timestamp
        if angle is None:
            self.missing=True;self.derivative=0.
            return {'angle':self.angle,'state':'unknown' if self.angle is None else 'held_unknown','limited':False,'source_verified':False}
        if self.angle is None:
            self.angle=self.raw=float(angle);self.missing=False
            return {'angle':self.angle,'state':'observed','limited':False,'source_verified':False}
        if dt is None:
            return {'angle':self.angle,'state':'prior_unknown','limited':False,'source_verified':False}
        # Unwrap consecutive observations against their own history. The render
        # guide may lag by several turns under its authored speed limit.
        # After unknown input, lost turn count is not observable: reacquire the
        # branch nearest the held render prior and reset the derivative below.
        anchor=self.angle if self.missing else self.raw
        raw=anchor+math.atan2(math.sin(angle-anchor),math.cos(angle-anchor))
        previous=self.angle;reacquiring=self.missing
        derivative=0. if self.missing else (raw-self.raw)/dt
        a=self.alpha(dt,self.dc);self.derivative+=a*(derivative-self.derivative)
        a=self.alpha(dt,self.min_cutoff+self.beta*abs(self.derivative))
        target=previous+a*(raw-previous);step=target-previous;bound=self.max_speed*dt
        limited=abs(step)>bound
        self.angle=previous+max(-bound,min(bound,step));self.raw=raw;self.missing=False
        return {'angle':self.angle,'state':'reacquiring' if reacquiring or limited else 'filtered','limited':limited,'source_verified':False}


def direction_consensus(directions,weights,*,threshold_degrees=30.,min_weight=.2):
    """Reject substantial disagreements instead of averaging a fake limb.

    Caller supplies same-role/same-time/same-bone candidates. Weak context
    boundaries are excluded by relative weight. Conflicts have NO direction;
    choosing a depth branch requires independent evidence upstream.
    """
    if not directions or len(directions)!=len(weights):raise ValueError('Aligned nonempty candidates required')
    if not 0<=min_weight<=1 or not 0<=threshold_degrees<=180:raise ValueError('Invalid gate thresholds')
    if any(not math.isfinite(w) or w<0 for w in weights) or sum(weights)<=0:raise ValueError('Finite nonnegative weights required')
    units=[]
    for v in directions:
        if len(v)!=3 or not all(math.isfinite(x) for x in v) or math.dist(v,[0,0,0])<1e-9:raise ValueError('Nondegenerate finite xyz directions required')
        n=math.dist(v,[0,0,0]);units.append([x/n for x in v])
    total=sum(weights);active=[k for k,w in enumerate(weights) if w>0 and w/total>=min_weight]
    if not active:active=[max(range(len(weights)),key=weights.__getitem__)]
    dot=lambda a,b:sum(x*y for x,y in zip(a,b))
    maximum=max([math.degrees(math.acos(max(-1.,min(1.,dot(units[a],units[b]))))) for a in active for b in active]+[0.])
    result={'active_candidates':active,'max_disagreement_degrees':maximum,'source_verified':False}
    if maximum>threshold_degrees:return dict(result,state='conflict',direction=None)
    v=[sum(units[k][j]*weights[k] for k in active) for j in range(3)];n=math.dist(v,[0,0,0])
    if n<1e-9:return dict(result,state='conflict',direction=None)
    return dict(result,state='consensus',direction=[x/n for x in v])


def _landmarks(points,scores,count,grid_px):
    if len(points)!=count or len(scores)!=count or not math.isfinite(grid_px) or grid_px<=0:
        raise ValueError('Landmark schema and positive grid required')
    if any(len(p)!=2 or not all(math.isfinite(x) for x in p) for p in points) or not all(math.isfinite(s) for s in scores):
        raise ValueError('Finite landmarks and scores required')


def face_controls(points,scores,*,grid_px,minimum_score=.35):
    """Scale/roll invariant aperture cues, NOT emotion or facial 3D recovery.

    Require enough spatial support for eye/inner-mouth ratios. A high network
    score on a sub-cell mouth is insufficient. No fabricated smile or blinks.
    """
    _landmarks(points,scores,68,grid_px)
    ids=[36,37,38,39,40,41,42,43,44,45,46,47,60,62,64,66]
    dist=lambda a,b:math.dist(points[a],points[b])
    eye_widths=[dist(36,39),dist(42,45)];mouth_width=dist(60,64)
    if min(scores[j] for j in ids)<minimum_score or min(eye_widths)<3*grid_px or mouth_width<4*grid_px:
        return {'state':'unresolved','face':None,'source_verified':False,'reason':'confidence or spatial support insufficient'}
    clamp=lambda v:max(0.,min(1.,v))
    left=(dist(43,47)+dist(44,46))/(2*eye_widths[1])
    right=(dist(37,41)+dist(38,40))/(2*eye_widths[0])
    return {'state':'resolved','face':{'smile':0.,'mouth':clamp(dist(62,66)/mouth_width*2),
            'eyeL':clamp(left/.35),'eyeR':clamp(right/.35)},'source_verified':False,
            'reason':'authored ratio-to-rig aperture mapping; no emotion classification'}


def palm_axis(points,scores,*,grid_px,minimum_score=.35):
    """Use wrist-to-MCP centroid direction; fingers remain canine anatomy."""
    _landmarks(points,scores,21,grid_px)
    ids=[0,5,9,13,17];center=[sum(points[j][k] for j in ids[1:])/4 for k in range(2)]
    angle=resolved_axis([center[k]-points[0][k] for k in range(2)],min(scores[j] for j in ids),grid_px=grid_px,minimum_score=minimum_score)
    return {'angle':angle,'state':'resolved' if angle is not None else 'unresolved','depth_observed':False,'source_verified':False}
