"""CPU-only derived motion controls. Raw pose observations remain unchanged."""
from __future__ import annotations
import math


def _vec(v):
    if len(v)!=3 or not all(math.isfinite(x) for x in v):
        raise ValueError('Finite xyz required')
    return [float(x) for x in v]


def _dot(a,b):return sum(x*y for x,y in zip(a,b))
def _sub(a,b):return [x-y for x,y in zip(a,b)]
def _cross(a,b):return [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]]
def _norm(v):return math.sqrt(_dot(v,v))


def _unit(v):
    length=_norm(v)
    if length<1e-12:raise ValueError('Nondegenerate vector required')
    return [x/length for x in v]


def _perpendicular(v,axis):return [x-_dot(v,axis)*y for x,y in zip(v,axis)]


class BendGuide:
    """Parallel-transport a bend pole; weak straight-leg hints remain unknown.

    Output is a retargeting control, not observed 3D truth. A supplied reliable
    knee can reacquire with an explicit angular-speed guard. Caller must test
    source projection, amplitudes, phase and actual meshes before admitting it.
    """
    def __init__(self,*,min_bend_ratio=.03,max_speed=math.pi*3,min_confidence=.5):
        if not all(math.isfinite(x) for x in [min_bend_ratio,max_speed,min_confidence]) or not 0<min_bend_ratio<.5 or max_speed<=0 or not 0<=min_confidence<=1:
            raise ValueError('Explicit finite bend/clock/confidence policy required')
        self.min_bend_ratio=min_bend_ratio;self.max_speed=max_speed;self.min_confidence=min_confidence
        self._time=None;self._axis=None;self._pole=None

    def update(self,time,root,knee,tip,*,confidence):
        root,knee,tip=map(_vec,(root,knee,tip))
        if not math.isfinite(time) or not math.isfinite(confidence) or not 0<=confidence<=1 or (self._time is not None and time<=self._time):
            raise ValueError('Strictly increasing finite seconds and confidence in [0,1] required')
        axis=_unit(_sub(tip,root));length=math.dist(root,knee)+math.dist(knee,tip)
        if length<1e-9:raise ValueError('Nondegenerate limb required')
        radial=_perpendicular(_sub(knee,root),axis);ratio=_norm(radial)/length
        candidate=_unit(radial) if ratio>=self.min_bend_ratio and confidence>=self.min_confidence else None
        half_turn=False;held=None
        if self._pole is not None:
            c=max(-1.,min(1.,_dot(self._axis,axis)));v=_cross(self._axis,axis)
            if c< -1+1e-8:
                # At an exact half turn, rotation around the previous pole maps
                # the old axis onto the new one while retaining that pole.
                half_turn=True;held=self._pole[:]
            else:
                first=_cross(v,self._pole);second=_cross(v,first)
                held=[self._pole[k]+first[k]+second[k]/(1+c) for k in range(3)]
            held=_unit(_perpendicular(held,axis))
        state='unknown';pole=None;observed_angle=None
        if held is None and candidate is not None:pole=candidate;state='first_reliable_bend'
        elif held is not None:
            pole=held;state='held_weak_bend'
            if candidate is not None:
                angle=math.atan2(_dot(axis,_cross(held,candidate)),max(-1.,min(1.,_dot(held,candidate))))
                observed_angle=angle;limit=self.max_speed*(time-self._time)
                step=max(-limit,min(limit,angle));cross=_cross(axis,held)
                pole=_unit([held[k]*math.cos(step)+cross[k]*math.sin(step) for k in range(3)])
                state='limited_reacquisition' if abs(angle)>limit+1e-10 else 'tracked'
        self._time=float(time);self._axis=axis;self._pole=pole
        return {'pole':pole,'state':state,'observed_bend_ratio':ratio,'observed_pole_delta_radians':observed_angle,
                'axis_half_turn_ambiguous':half_turn,'source_truth_verified':False}


class ContactGuide:
    """Hysteresis on explicit contact evidence, never a lowest-foot detector.

    Gap is in body units; speed is per second for the SAME material point.
    Unknown, pivoting and intentionally moving feet are never statically locked.
    Inferred evidence needs a separate opt-in before it may request a lock.
    """
    def __init__(self,*,enter_gap=.02,exit_gap=.05,enter_speed=.2,exit_speed=.4,
                 confirm_seconds=.1,min_confidence=.5,allow_inferred_lock=False):
        vals=[enter_gap,exit_gap,enter_speed,exit_speed,confirm_seconds,min_confidence]
        if not all(math.isfinite(x) for x in vals) or not 0<=enter_gap<exit_gap or not 0<=enter_speed<exit_speed or confirm_seconds<0 or not 0<=min_confidence<=1:
            raise ValueError('Finite ordered entry/exit policy required')
        self.enter_gap=enter_gap;self.exit_gap=exit_gap;self.enter_speed=enter_speed;self.exit_speed=exit_speed
        self.confirm_seconds=confirm_seconds;self.min_confidence=min_confidence;self.allow_inferred_lock=bool(allow_inferred_lock)
        self._time=None;self._start=None;self._active=False;self._kind=None

    def update(self,time,*,gap,speed,confidence,mode,evidence_kind):
        if mode not in ['stance','swing','pivot','moving_contact','flight','unknown'] or evidence_kind not in ['unknown','inferred','source_confirmed']:
            raise ValueError('Explicit source mode and evidence provenance required')
        if not all(math.isfinite(x) for x in [time,gap,speed,confidence]) or speed<0 or not 0<=confidence<=1 or (self._time is not None and time<=self._time):
            raise ValueError('Finite increasing seconds, nonnegative speed and confidence required')
        if evidence_kind!=self._kind:self._active=False;self._start=None
        if mode=='unknown' or evidence_kind=='unknown' or confidence<self.min_confidence:
            state='unknown';self._active=False;self._start=None
        elif mode!='stance':state=mode;self._active=False;self._start=None
        else:
            valid=abs(gap)<=(self.exit_gap if self._active else self.enter_gap) and speed<=(self.exit_speed if self._active else self.enter_speed)
            if not valid:self._active=False;self._start=None;state='unresolved_stance'
            else:
                if self._start is None:self._start=time
                self._active=self._active or time-self._start>=self.confirm_seconds-1e-12
                state='stance_candidate' if self._active else 'pending_stance'
        allowed=evidence_kind=='source_confirmed' or (evidence_kind=='inferred' and self.allow_inferred_lock)
        self._time=float(time);self._kind=evidence_kind
        return {'state':state,'lock_weight':float(self._active and allowed),'evidence_kind':evidence_kind,
                'inferred_lock_opt_in':self.allow_inferred_lock,'source_confirmation_supplied':evidence_kind=='source_confirmed',
                'source_truth_verified':False}


def gate_contact_target_plan(plan,*,allow_inferred_lock=False):
    """Gate FINAL position-lock weights after any caller-authored fades.

    A fade must not extend a future stance into unknown/moving frames. This
    final boundary check complements ContactGuide; it does not derive support,
    choose an anchor, adjust a pose, or certify caller-supplied source labels.
    The caller still checks projection, reach and full-event continuity.
    One call covers one identity and one fixed contact material point.
    """
    rows=[];blocked=[];last=-1
    for item in plan:
        frame=item.get('frame');mode=item.get('mode');kind=item.get('evidence_kind')
        weight=item.get('weight');target=item.get('target')
        if (isinstance(frame,bool) or not isinstance(frame,int) or frame<=last or
            mode not in ['stance','swing','pivot','moving_contact','flight','unknown'] or
            kind not in ['unknown','inferred','source_confirmed'] or
            not isinstance(weight,(int,float)) or not math.isfinite(weight) or not 0<=weight<=1):
            raise ValueError('Ordered frames, explicit source state/provenance and finite lock weight required')
        point=_vec(target)
        allowed=mode=='stance' and (kind=='source_confirmed' or (kind=='inferred' and allow_inferred_lock))
        row=dict(item,target=point,requested_weight=float(weight),weight=float(weight) if allowed else 0.,
                 lock_authorized=bool(allowed),source_truth_verified=False)
        if weight and not allowed:blocked.append(frame)
        rows.append(row);last=frame
    return {'rows':rows,'blocked_frames':blocked,'inferred_lock_opt_in':bool(allow_inferred_lock),
            'source_truth_verified':False}


def point_velocity(previous,current,dt,normal,*,body_scale):
    """Velocity of a caller-selected fixed material point, not a new min vertex."""
    previous,current,normal=map(_vec,(previous,current,normal))
    if not math.isfinite(dt) or dt<=0 or not math.isfinite(body_scale) or body_scale<=0:
        raise ValueError('Positive finite seconds and body scale required')
    axis=_unit(normal);v=[x/dt/body_scale for x in _sub(current,previous)]
    signed=_dot(v,axis);tangent=_perpendicular(v,axis)
    return {'tangent_units_per_second':_norm(tangent),'normal_units_per_second':signed,'contact_verified':False}
