"""Chopster Auto Camera Director.

Principle: the camera is a *shot*, not a face-following cursor.
Once a target is selected, the crop is locked. Small head/body movements do not
move the frame. Camera movement only happens when the director changes shot.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class CameraPoint:
    t: float
    x: float
    y: float
    source: str = "center"
    confidence: float = 0.5
    subject_left: float | None = None
    subject_right: float | None = None
    subject_top: float | None = None
    subject_bottom: float | None = None
    group_left: float | None = None
    group_right: float | None = None
    group_top: float | None = None
    group_bottom: float | None = None
    target_id: str | None = None
    shot: str = "solo"
    hold: bool = True
    reason: str = ""


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def _smoothstep(x: float) -> float:
    x = _clamp(x)
    return x * x * (3.0 - 2.0 * x)


def normalize_points(points: Iterable[dict | object]) -> list[CameraPoint]:
    out: list[CameraPoint] = []
    for raw in points or []:
        try:
            get = raw.get if isinstance(raw, dict) else lambda k, d=None: getattr(raw, k, d)
            out.append(CameraPoint(
                t=max(0.0, float(get("t", 0.0))), x=_clamp(float(get("x", .5))), y=_clamp(float(get("y", .5))),
                source=str(get("source", "center") or "center"), confidence=_clamp(float(get("confidence", .5))),
                subject_left=None if get("subject_left", None) is None else _clamp(float(get("subject_left"))),
                subject_right=None if get("subject_right", None) is None else _clamp(float(get("subject_right"))),
                subject_top=None if get("subject_top", None) is None else _clamp(float(get("subject_top"))),
                subject_bottom=None if get("subject_bottom", None) is None else _clamp(float(get("subject_bottom"))),
                group_left=None if get("group_left", None) is None else _clamp(float(get("group_left"))),
                group_right=None if get("group_right", None) is None else _clamp(float(get("group_right"))),
                group_top=None if get("group_top", None) is None else _clamp(float(get("group_top"))),
                group_bottom=None if get("group_bottom", None) is None else _clamp(float(get("group_bottom"))),
                target_id=None if get("target_id", None) is None else str(get("target_id")),
                shot=str(get("shot", "solo") or "solo"), hold=bool(get("hold", True)),
                reason=str(get("reason", "") or ""),
            ))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p.t)
    dedup: list[CameraPoint] = []
    for p in out:
        if dedup and abs(p.t - dedup[-1].t) < 1e-4:
            if p.confidence >= dedup[-1].confidence:
                dedup[-1] = p
        else:
            dedup.append(p)
    return dedup


def _distance(a: CameraPoint, b: CameraPoint) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** .5


def stabilize_camera_path(points: Iterable[dict | object], *, dead_zone: float = .035,
                          min_shot_duration: float = 1.50, transition_duration: float = .32,
                          max_speed: float = .55, max_step: float = .12) -> list[dict]:
    """Convert noisy detector samples into locked editorial shots.

    Unlike the old implementation, same-target points do *not* continuously move
    the crop. The target position is sampled when a shot starts and held until a
    real shot change is accepted. This eliminates micro-panning/jitter.
    """
    src = normalize_points(points)
    if not src:
        return []

    locked: list[CameraPoint] = []
    active = src[0]
    active_since = src[0].t
    pending: CameraPoint | None = None
    pending_since = src[0].t

    for p in src:
        # Same editorial target: keep the exact locked crop. The face can move;
        # the camera does not chase it.
        same_target = (
            p.target_id and active.target_id and p.target_id == active.target_id
        ) or (not p.target_id and p.source == active.source)
        same_shot = p.shot == active.shot
        if same_target and same_shot:
            locked.append(CameraPoint(p.t, active.x, active.y, active.source,
                                      max(active.confidence, p.confidence), active.subject_left, active.subject_right,
                                      active.subject_top, active.subject_bottom, active.group_left,
                                      active.group_right, active.group_top, active.group_bottom,
                                      active.target_id, active.shot, True, active.reason))
            continue

        # Require a new target to persist before switching. This is hysteresis.
        candidate_strength = p.confidence
        if pending is None or pending.target_id != p.target_id or pending.shot != p.shot:
            pending = p
            pending_since = p.t
        if (p.t - pending_since) < min_shot_duration or candidate_strength < max(.52, active.confidence - .08):
            locked.append(CameraPoint(p.t, active.x, active.y, active.source,
                                      active.confidence, active.subject_left, active.subject_right,
                                      active.subject_top, active.subject_bottom, active.group_left, active.group_right,
                                      active.group_top, active.group_bottom, active.target_id, active.shot, True, active.reason))
            continue
        active = pending
        active_since = p.t
        pending = None
        locked.append(active)

    # A final AI target can appear only on the last sampled frame. Flush a strong
    # pending editorial switch so the chosen target is not discarded merely because
    # there was no later detector sample to satisfy the hysteresis timer.
    if pending is not None and pending.target_id != active.target_id:
        elapsed = max(0.0, src[-1].t - pending_since)
        if elapsed >= min_shot_duration or pending.confidence >= 0.85:
            active = pending
            locked.append(active)

    # Build a sparse shot-change path. Same-shot samples are collapsed to one
    # keyframe; only an accepted editorial switch gets a transition.
    changes: list[CameraPoint] = []
    for p in locked:
        if not changes:
            changes.append(p)
            continue
        q = changes[-1]
        if p.target_id != q.target_id or p.shot != q.shot or (not p.target_id and p.source != q.source):
            changes.append(p)

    # Generate a short eased move at a shot switch, then hold the new target.
    out: list[CameraPoint] = []
    for i, p in enumerate(changes):
        if i == 0:
            out.append(p)
            continue
        prev = changes[i - 1]
        switch_t = p.t
        # Do not create a long pan. The camera moves over a short cinematic
        # transition, then becomes completely static on the new target.
        dur = min(transition_duration, max(.10, switch_t - prev.t) * .65)
        move_start = max(prev.t, switch_t - dur)
        # Replace/append transition endpoints without altering the shot timing.
        if out and abs(out[-1].t - move_start) > 1e-4:
            out.append(CameraPoint(move_start, prev.x, prev.y, prev.source, prev.confidence,
                                   prev.subject_left, prev.subject_right, prev.subject_top, prev.subject_bottom,
                                   prev.group_left, prev.group_right, prev.group_top, prev.group_bottom,
                                   prev.target_id, prev.shot, True, prev.reason))
        elif out:
            out[-1] = CameraPoint(move_start, prev.x, prev.y, prev.source, prev.confidence,
                                  prev.subject_left, prev.subject_right, prev.subject_top, prev.subject_bottom,
                                  prev.group_left, prev.group_right, prev.group_top, prev.group_bottom,
                                  prev.target_id, prev.shot, True, prev.reason)
        # Materialize a few bounded transition keyframes. The renderer can then
        # interpolate between small deltas instead of receiving a giant coordinate
        # jump at the shot boundary. Once the endpoint is reached, no more keyframes
        # are generated until the next editorial shot change.
        dx, dy = p.x - prev.x, p.y - prev.y
        dist = (dx * dx + dy * dy) ** .5
        steps = max(1, int(__import__("math").ceil(dist / .04)))
        for k in range(1, steps + 1):
            u = k / steps
            eased = _smoothstep(u)
            tt = move_start + (switch_t - move_start) * u
            out.append(CameraPoint(tt, prev.x + dx * eased, prev.y + dy * eased,
                                   p.source if k == steps else prev.source,
                                   p.confidence if k == steps else prev.confidence,
                                   p.subject_left if k == steps else prev.subject_left,
                                   p.subject_right if k == steps else prev.subject_right,
                                   p.subject_top if k == steps else prev.subject_top,
                                   p.subject_bottom if k == steps else prev.subject_bottom,
                                   p.group_left if k == steps else prev.group_left,
                                   p.group_right if k == steps else prev.group_right,
                                   p.group_top if k == steps else prev.group_top,
                                   p.group_bottom if k == steps else prev.group_bottom,
                                   p.target_id if k == steps else prev.target_id,
                                   p.shot if k == steps else prev.shot, True, p.reason))

    # Deduplicate and serialize.
    compact: list[CameraPoint] = []
    for p in out:
        if compact and abs(p.t - compact[-1].t) < 1e-4:
            compact[-1] = p
        else:
            compact.append(p)
    return [{
        "t": round(p.t, 4), "x": round(_clamp(p.x), 6), "y": round(_clamp(p.y), 6),
        "source": p.source, "confidence": round(_clamp(p.confidence), 3),
        "target_id": p.target_id, "shot": p.shot, "hold": True, "reason": p.reason,
        **({"subject_left": round(_clamp(p.subject_left), 6)} if p.subject_left is not None else {}),
        **({"subject_right": round(_clamp(p.subject_right), 6)} if p.subject_right is not None else {}),
        **({"subject_top": round(_clamp(p.subject_top), 6)} if p.subject_top is not None else {}),
        **({"subject_bottom": round(_clamp(p.subject_bottom), 6)} if p.subject_bottom is not None else {}),
        **({"group_left": round(_clamp(p.group_left), 6)} if p.group_left is not None else {}),
        **({"group_right": round(_clamp(p.group_right), 6)} if p.group_right is not None else {}),
        **({"group_top": round(_clamp(p.group_top), 6)} if p.group_top is not None else {}),
        **({"group_bottom": round(_clamp(p.group_bottom), 6)} if p.group_bottom is not None else {}),
    } for p in compact]


def interpolate_focus(points: Iterable[dict | object], t: float) -> tuple[float, float]:
    pts = normalize_points(points)
    if not pts:
        return .5, .5
    t = max(0., float(t))
    if t <= pts[0].t: return pts[0].x, pts[0].y
    if t >= pts[-1].t: return pts[-1].x, pts[-1].y
    lo, hi = 0, len(pts)-1
    while lo + 1 < hi:
        mid = (lo + hi)//2
        if pts[mid].t <= t: lo = mid
        else: hi = mid
    a,b=pts[lo],pts[hi]
    if b.t <= a.t: return a.x,a.y
    u=_smoothstep((t-a.t)/(b.t-a.t))
    return _clamp(a.x+(b.x-a.x)*u), _clamp(a.y+(b.y-a.y)*u)


def make_clip_relative(points: Iterable[dict | object], clip_start: float, clip_end: float | None = None) -> list[dict]:
    start=max(0.,float(clip_start)); end=None if clip_end is None else max(start,float(clip_end))
    pts=normalize_points(points); out=[]
    for p in pts:
        if p.t < start-.75 or (end is not None and p.t > end+.75): continue
        q={"t":max(0.,p.t-start),"x":p.x,"y":p.y,"source":p.source,"confidence":p.confidence,
           "target_id":p.target_id,"shot":p.shot,"hold":True,"reason":p.reason}
        for key in ("subject_left","subject_right","subject_top","subject_bottom","group_left","group_right","group_top","group_bottom"):
            val=getattr(p,key,None)
            if val is not None:q[key]=val
        out.append(q)
    if not out:
        x,y=interpolate_focus(pts,start)
        out=[{"t":0.,"x":x,"y":y,"source":"interpolated","confidence":.45,"shot":"fit","hold":True}]
    elif out[0]["t"] > .001:
        x,y=interpolate_focus(pts,start)
        first=dict(out[0]); first["t"]=0.; first["x"]=x; first["y"]=y; first["source"]="clip-start"; out.insert(0,first)
    return out


def _extract_json(text: str) -> Any:
    raw=(text or "").strip()
    m=re.search(r"```(?:json)?\s*(.*?)\s*```",raw,re.S|re.I)
    if m: raw=m.group(1).strip()
    return json.loads(raw)


def ai_camera_director(orchestrator, timeline: list[dict], transcript_text: str = "", max_tokens: int = 6000) -> list[dict]:
    """Select editorial shot changes through the central AI Orchestrator.

    The AI may choose target/shot/time only. Local vision remains authoritative for
    coordinates and safe composition. If the remote AI fails, the deterministic
    local director returns the existing target changes instead of stopping.
    """
    if orchestrator is None or not timeline:
        return []
    from chopster.ai.provider_interface import AIRequest
    compressed=[]
    prev_key=None
    for row in timeline:
        key=(str(row.get("target_id")), str(row.get("shot")), str(row.get("active_speaker")))
        if key != prev_key:
            compressed.append(row); prev_key=key
        elif len(compressed) < 240 and (not compressed or float(row.get("t",0)) - float(compressed[-1].get("t",0)) >= 8.0):
            compressed.append(row)
    compressed=(compressed or timeline[:240])[:300]
    payload={"timeline":compressed,"transcript":transcript_text[:12000]}
    prompt=(
        "Kamu adalah Camera Director untuk video Shorts. Pilih pergantian shot yang benar-benar diperlukan "
        "dari data timeline. JANGAN membuat koordinat x/y. Kamera harus diam ketika orang yang sama berbicara/reaksi. "
        "Ganti shot hanya untuk pembicara aktif, reaksi penting, atau komposisi group yang lebih masuk akal. "
        "Jangan berpindah hanya karena wajah bergeser sedikit. Jika group terlalu lebar untuk target portrait/vertical canvas, gunakan shot=fit. "
        "Kembalikan JSON array saja: [{start,end,target_id,shot,reason}]. shot hanya solo,two,group,fit. Durasi shot minimal 1.5 detik.\n\nDATA:\n" + json.dumps(payload,ensure_ascii=False)
    )
    def local_plan():
        rows=[]; last=None; start=None; last_reason="Local Camera Director"
        for r in compressed:
            tid=str(r.get("target_id") or ""); shot=str(r.get("shot") or "solo")
            key=(tid,shot)
            t=float(r.get("t") or 0)
            if key != last:
                if last is not None and start is not None:
                    rows.append({"start":start,"end":t,"target_id":last[0],"shot":last[1],"reason":last_reason})
                last=key; start=t
                last_reason="Local evidence: target/shot changed"
        if last is not None and start is not None:
            end=max(start+1.5,float(compressed[-1].get("t") or start)+1.5)
            rows.append({"start":start,"end":end,"target_id":last[0],"shot":last[1],"reason":last_reason})
        return rows
    def local_json():
        return json.dumps(local_plan(), ensure_ascii=False)
    try:
        resp=orchestrator.generate(AIRequest(prompt=prompt,system="Kamu editor kamera yang konservatif dan stabil.",temperature=.15,max_tokens=max_tokens,timeout=90), local_fallback=local_json)
        data=_extract_json(resp.text)
        rows=data if isinstance(data,list) else data.get("shots",[]) if isinstance(data,dict) else []
        return [r for r in rows if isinstance(r,dict) and r.get("target_id") is not None]
    except Exception:
        return local_plan()




def _coerce_target_id(target: str, faces: list[dict]) -> str:
    """Map a model-friendly person label back to the local tracker ID."""
    raw=str(target or "").strip().lower()
    if not raw:
        return ""
    # Exact tracker ID first.
    for f in faces:
        if str(f.get("id")) == raw or str(f.get("id")) == str(target):
            return str(f.get("id"))
    # Common model labels: person 1 / speaker 2 / subject-3.
    import re
    m=re.search(r"(?:person|speaker|subject|face|target)[\s_-]*(\d+)$", raw)
    if not m:
        m=re.fullmatch(r"#?(\d+)", raw)
    if m:
        idx=int(m.group(1))
        ordered=sorted(faces, key=lambda f: (float(f.get("x", .5)), str(f.get("id"))))
        if 1 <= idx <= len(ordered):
            return str(ordered[idx-1].get("id"))
    return ""


def _local_motion_shots(timeline: list[dict], *, min_switch_seconds: float = 2.0) -> list[dict]:
    """Build a deterministic speaker-like camera plan from mouth-motion evidence.

    This is the last local safety net when Gemini is unavailable or returns no valid
    visual decisions. It only switches after the same candidate remains dominant for
    at least ``min_switch_seconds`` and otherwise preserves the current shot lock.
    """
    rows=[]
    current=None
    start=None
    pending=None
    pending_since=None
    for row in timeline or []:
        t=float(row.get("t",0.0) or 0.0)
        faces=list(row.get("faces") or [])
        ranked=[]
        for f in faces:
            try:
                ranked.append((float(f.get("motion",0.0) or 0.0), str(f.get("id"))))
            except Exception:
                continue
        if not ranked:
            continue
        ranked.sort(reverse=True)
        best_motion,best_id=ranked[0]
        # Require visible mouth activity; otherwise don't manufacture a switch.
        candidate = best_id if best_motion >= 1.0 else current
        if current is None and candidate:
            current=candidate; start=t
            pending=None; pending_since=None
            continue
        if candidate == current or not candidate:
            continue
        if pending != candidate:
            pending=candidate; pending_since=t
            continue
        if pending_since is not None and t-pending_since >= min_switch_seconds:
            if start is not None:
                rows.append({"start":start,"end":t,"target_id":current,"shot":"solo","confidence":0.82,"reason":"Local mouth-motion speaker switch"})
            current=pending; start=pending_since
            pending=None; pending_since=None
    if current is not None and start is not None:
        end=max(start+1.5, float((timeline or [{}])[-1].get("t",start) or start)+1.5)
        rows.append({"start":start,"end":end,"target_id":current,"shot":"solo","confidence":0.82,"reason":"Local mouth-motion speaker hold"})
    return rows


def apply_ai_shots(points: list[dict], shots: list[dict], timeline: list[dict] | None = None, *, source_aspect: float | None = None, target_aspect: float | None = None) -> list[dict]:
    """Apply an editorial shot plan and make it physically move to the chosen subject.

    The model is only allowed to choose a subject/shot. Local tracker coordinates are
    authoritative. Human-friendly labels are mapped back to tracker IDs, and the
    adaptive-framing rule is re-evaluated after every target change so a close face
    cannot accidentally become a giant crop.
    """
    if not points or not shots:
        return points
    timeline=list(timeline or [])

    def nearest_row(t: float):
        if not timeline:
            return None
        return min(timeline, key=lambda r: abs(float(r.get("t",0.0))-t))

    out=[]
    shots_sorted=sorted([dict(x) for x in shots if isinstance(x,dict)], key=lambda x: float(x.get("start",0) or 0))

    for p in points:
        t=float(p.get("t",0) or 0)
        # Choose the latest matching shot so overlapping segments do not pin the camera
        # forever to the first interval.
        chosen=None
        for sh in shots_sorted:
            try:
                if float(sh.get("start",0) or 0) <= t <= float(sh.get("end",0) or 0):
                    chosen=sh
            except Exception:
                continue
        q=dict(p)
        if chosen:
            row=nearest_row(t)
            faces=list(row.get("faces") or []) if row else []
            shot=str(chosen.get("shot") or q.get("shot") or "solo")
            requested=str(chosen.get("target_id") or q.get("target_id") or "")
            mapped=_coerce_target_id(requested, faces) if faces else requested
            q["shot"]=shot
            q["confidence"]=max(float(q.get("confidence",0.5) or 0.5), float(chosen.get("confidence",0.92) or 0.92))
            q["reason"]=str(chosen.get("reason") or "AI Camera Director")
            if row and faces:
                if shot in {"group","two","fit"} and faces:
                    selected=faces
                    # If AI named a target, keep the target plus the most relevant partner
                    # rather than blindly using every detected face.
                    if mapped and any(str(f.get("id"))==mapped for f in faces):
                        main=next(f for f in faces if str(f.get("id"))==mapped)
                        others=[f for f in faces if str(f.get("id"))!=mapped]
                        selected=[main]+sorted(others, key=lambda f: abs(float(f.get("x",.5))-float(main.get("x",.5))))[:1]
                    gl=min(float(f.get("left",.5)) for f in selected); gr=max(float(f.get("right",.5)) for f in selected)
                    gt=min(float(f.get("top",.5)) for f in selected); gb=max(float(f.get("bottom",.5)) for f in selected)
                    q.update({"x":(gl+gr)*.5,"y":(gt+gb)*.5,"group_left":gl,"group_right":gr,"group_top":gt,"group_bottom":gb})
                    if mapped:q["target_id"]=mapped
                else:
                    match=next((f for f in faces if str(f.get("id"))==mapped),None)
                    if match is not None:
                        q.update({"x":float(match.get("x",q.get("x",.5))),"y":float(match.get("y",q.get("y",.5))),
                                  "subject_left":match.get("left"),"subject_right":match.get("right"),
                                  "subject_top":match.get("top"),"subject_bottom":match.get("bottom"),
                                  "target_id":str(match.get("id"))})
            # Re-evaluate close-subject safety after the AI move.
            if source_aspect and target_aspect and q.get("target_id"):
                try:
                    from chopster.clipper.adaptive_framing import decide_framing
                    d=decide_framing(q, source_aspect=float(source_aspect), target_aspect=float(target_aspect))
                    q["x"]=d.center_x; q["y"]=d.center_y
                    if d.mode=="adaptive_fit":
                        q["shot"]="fit"; q["reason"]=((q.get("reason") or "AI Camera Director")+" • "+d.reason).strip()
                except Exception:
                    pass
        out.append(q)

    # Materialize editorial boundaries even if the tracker sample grid does not land
    # exactly on the AI-selected start time. This guarantees that an AI shot decision
    # becomes a real camera change instead of waiting for a coincidental detector frame.
    if timeline and shots_sorted and out:
        existing_times={round(float(x.get("t",0.0)),4) for x in out}
        for sh in shots_sorted:
            try:
                st=float(sh.get("start",0.0) or 0.0)
            except Exception:
                continue
            if st <= float(out[0].get("t",0.0))+1e-4 or st >= float(out[-1].get("t",0.0))-1e-4:
                continue
            key=round(st,4)
            if key in existing_times:
                continue
            anchor=min(out,key=lambda x: abs(float(x.get("t",0.0))-st))
            q=dict(anchor); q["t"]=st
            row=nearest_row(st); faces=list(row.get("faces") or []) if row else []
            shot=str(sh.get("shot") or q.get("shot") or "solo")
            target=_coerce_target_id(str(sh.get("target_id") or q.get("target_id") or ""), faces) if faces else str(sh.get("target_id") or q.get("target_id") or "")
            q["shot"]=shot; q["target_id"]=target; q["reason"]=str(sh.get("reason") or q.get("reason") or "AI Camera Director")
            if row and faces:
                if shot in {"group","two","fit"}:
                    chosen=faces
                    if target:
                        m=next((f for f in faces if str(f.get("id"))==target),None)
                        if m:
                            others=[f for f in faces if str(f.get("id"))!=target]
                            chosen=[m]+sorted(others,key=lambda f: abs(float(f.get("x",.5))-float(m.get("x",.5))))[:1]
                    gl=min(float(f.get("left",.5)) for f in chosen); gr=max(float(f.get("right",.5)) for f in chosen)
                    gt=min(float(f.get("top",.5)) for f in chosen); gb=max(float(f.get("bottom",.5)) for f in chosen)
                    q.update({"x":(gl+gr)*.5,"y":(gt+gb)*.5,"group_left":gl,"group_right":gr,"group_top":gt,"group_bottom":gb})
                else:
                    m=next((f for f in faces if str(f.get("id"))==target),None)
                    if m:
                        q.update({"x":float(m.get("x",q.get("x",.5))),"y":float(m.get("y",q.get("y",.5))),
                                  "subject_left":m.get("left"),"subject_right":m.get("right"),"subject_top":m.get("top"),"subject_bottom":m.get("bottom")})
            out.append(q); existing_times.add(key)
        out.sort(key=lambda x: float(x.get("t",0.0)))
    return out


def ai_camera_director_visual(
    orchestrator,
    timeline: list[dict],
    transcript_text: str = "",
    image_paths: list[str] | None = None,
    image_times: list[float] | None = None,
    visual_context: dict | None = None,
    max_tokens: int = 7000,
) -> list[dict]:
    """Use representative frames to make explicit per-frame camera decisions.

    Older versions asked for only sparse shot-change timestamps. That often returned
    one static decision, so the AI could be online while the camera still never moved.
    This version forces one decision per representative frame and then compacts those
    decisions into stable shots.
    """
    if orchestrator is None or not timeline:
        return []

    from chopster.ai.provider_interface import AIRequest, AIResponse
    times=[float(x) for x in (image_times or [])]
    if not times:
        return _local_motion_shots(timeline)

    frame_maps=[]
    for i,t in enumerate(times):
        row=min(timeline,key=lambda r: abs(float(r.get("t",0.0))-t)) if timeline else {}
        candidates=[]
        for f in list(row.get("faces") or []):
            candidates.append({
                "id":str(f.get("id")),"x":round(float(f.get("x",.5)),3),"y":round(float(f.get("y",.5)),3),
                "left":round(float(f.get("left",0)),3),"right":round(float(f.get("right",1)),3),
                "top":round(float(f.get("top",0)),3),"bottom":round(float(f.get("bottom",1)),3),
                "motion":round(float(f.get("motion",0)),2),
            })
        frame_maps.append({"frame":i+1,"time":round(t,2),"candidate_subjects":candidates})

    labels="\n".join(f"Frame {i+1} = {t:.2f}s" for i,t in enumerate(times))
    prompt=(
        "Kamu adalah Gemini Visual Camera Director untuk editor Shorts. Lihat SEMUA frame yang dikirim. "
        "Untuk SETIAP frame, keluarkan SATU keputusan eksplisit memakai salah satu id subject yang ada di FRAME SUBJECT MAP. "
        "Tujuan: fokus ke orang yang sedang berbicara atau bereaksi penting, tanpa mengikuti gerakan kepala kecil. "
        "Jika dua orang terlalu dekat/terlalu besar untuk portrait, pilih shot=fit atau two. "
        "Jangan membuat id baru. Jika yakin speaker yang aktif berubah, target_id harus berubah. "
        "Kembalikan JSON object SAJA: {\"decisions\":[{\"frame\":1,\"target_id\":\"1\",\"shot\":\"solo\",\"confidence\":0.9,\"reason\":\"...\"}]} . "
        "Harus ada satu decision untuk setiap frame yang kamu lihat.\n\nFRAMES:\n"+labels+
        "\n\nFRAME SUBJECT MAP (ID LOKAL, WAJIB DIPAKAI):\n"+json.dumps(frame_maps,ensure_ascii=False)+
        "\n\nTIMELINE: "+json.dumps(timeline[:320],ensure_ascii=False)+
        "\n\nTRANSCRIPT: "+transcript_text[:12000]+
        "\n\nVISUAL CONTEXT: "+json.dumps(visual_context or {},ensure_ascii=False)[:7000]
    )
    req=AIRequest(prompt=prompt,system="Gunakan bukti visual frame. Jangan menebak ID baru.",temperature=.10,max_tokens=max_tokens,timeout=90)

    def local_plan():
        return _local_motion_shots(timeline)

    try:
        resp=orchestrator.generate_vision(
            prompt,image_paths or [],system=req.system,model=req.model,temperature=req.temperature,
            max_tokens=req.max_tokens or max_tokens,timeout=req.timeout,
            local_fallback=lambda: AIResponse(text=json.dumps({"decisions":[]}),raw={"provider":"local-fallback","fallback":True}),
        )
        data=_extract_json(resp.text)
        decisions=[]
        if isinstance(data,dict):
            decisions=data.get("decisions") or data.get("frame_decisions") or []
            if not decisions and isinstance(data.get("shots"),list):
                return [r for r in data["shots"] if isinstance(r,dict) and r.get("target_id") is not None]
        if not isinstance(decisions,list):
            decisions=[]

        normalized=[]
        for d in decisions:
            if not isinstance(d,dict):
                continue
            try:
                fi=int(d.get("frame"))-1
            except Exception:
                continue
            if fi<0 or fi>=len(times):
                continue
            row=frame_maps[fi]
            faces=row.get("candidate_subjects") or []
            tid=_coerce_target_id(str(d.get("target_id") or ""), faces)
            if not tid:
                continue
            shot=str(d.get("shot") or "solo").lower()
            if shot not in {"solo","two","group","fit"}: shot="solo"
            normalized.append({"frame":fi,"time":times[fi],"target_id":tid,"shot":shot,"confidence":float(d.get("confidence",.75) or .75),"reason":str(d.get("reason") or "Gemini visual camera decision")})
        normalized.sort(key=lambda x:x["frame"])
        # Fill missing frames using nearest valid decision, otherwise local plan.
        if len(normalized)<max(1,len(times)//2):
            return local_plan()
        by_frame={d["frame"]:d for d in normalized}
        if not normalized:
            return local_plan()
        last=normalized[0]
        completed=[]
        for i in range(len(times)):
            if i in by_frame: last=by_frame[i]
            completed.append(last|{"frame":i,"time":times[i]})
        # Compact consecutive identical decisions into real shot intervals.
        shots=[]; current=completed[0]; start_t=completed[0]["time"]
        for item in completed[1:]:
            same=(item["target_id"]==current["target_id"] and item["shot"]==current["shot"])
            if not same:
                end_t=float(item["time"])
                if end_t-start_t >= 1.2:
                    shots.append({"start":start_t,"end":end_t,"target_id":current["target_id"],"shot":current["shot"],"confidence":current.get("confidence",0.92),"reason":current["reason"]})
                    current=item; start_t=float(item["time"])
                else:
                    # Ignore tiny oscillations and keep the locked shot.
                    continue
        final_end=max(start_t+1.5,float(completed[-1]["time"])+1.5)
        shots.append({"start":start_t,"end":final_end,"target_id":current["target_id"],"shot":current["shot"],"confidence":current.get("confidence",0.92),"reason":current["reason"]})
        return shots or local_plan()
    except Exception:
        return local_plan()

