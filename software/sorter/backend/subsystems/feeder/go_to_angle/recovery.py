"""C3-only spatial evidence for a reserved transfer, using paired perception frames.

All distances are channel-output degrees. Box corners conservatively bound each
material footprint; the one-degree padding matches the calibrated arc resolution.
This module neither commands motors nor assigns physical piece counts.
"""
import math

from perception.arcs import exitOnlySections, _orderedCircularSections

UNCERTAINTY_DEG = 1.0


def capture_support(service):
    if service is None:
        return {}
    sample = service.read_pieces_and_frame(3)
    channel = service.channels().get(3)
    if sample is None or channel is None:
        return {}
    pieces, frame = sample
    sections = _orderedCircularSections(exitOnlySections(channel))
    if not sections or frame is None:
        return {}
    sign = -1 if channel.reverse else 1
    entry = sections[-1] if channel.reverse else sections[0]
    cx, cy = channel.center
    material = []
    for p in pieces:
        x1, y1, x2, y2 = p.bbox
        if x2 <= x1 or y2 <= y1 or x1 <= cx <= x2 and y1 <= cy <= y2:
            return {}  # No meaningful angular sweep for a box containing the axis.
        angle = math.degrees(math.atan2((y1+y2)/2-cy, (x1+x2)/2-cx))
        offsets = [((math.degrees(math.atan2(y-cy, x-cx))-angle+180) % 360)-180
                   for x in (x1, x2) for y in (y1, y2)]
        gap = float(p.com_forward_to_exit_deg)
        bounds = [gap-sign*d for d in offsets]
        if not all(math.isfinite(v) for v in bounds) or max(bounds)-min(bounds) >= 180:
            return {}
        radius_max = max(math.hypot(x-cx, y-cy) for x in (x1, x2) for y in (y1, y2))
        radius_min = math.hypot(max(x1-cx, 0, cx-x2), max(y1-cy, 0, cy-y2))
        radial_error = max(2.0, radius_max*math.sin(math.radians(UNCERTAINTY_DEG)))
        material.append({'id': p.sv_bt_track_id, 'bbox': list(p.bbox), 'com': gap,
                         'radius_low': radius_min-radial_error, 'radius_high': radius_max+radial_error,
                         'zone': p.zone_code, 'low': min(bounds)-UNCERTAINTY_DEG,
                         'high': max(bounds)+UNCERTAINTY_DEG})
    return {'ts': float(frame.timestamp), 'material': material, 'entry': entry,
            'exit_span_deg': float(len(sections)),
            'sign': sign, 'exit_sections': sorted(channel.exit_sections),
            'drop_sections': sorted(channel.drop_sections)}


def followthrough_to_exit_end(evidence, support, *, margin, maximum):
    """Propose travel from observed position, never proof of piece displacement.

    The rotor is friction driven. This geometric horizon is capped by the motor
    limit here and by follower clearance at dispatch. Only a later observation
    measures progress; only C4 confirms arrival.
    """
    material = support.get('material', [])
    span = evidence.get('exit_span_deg')
    if not material or span is None:
        return None
    values = [float(span), float(margin), float(maximum)]
    gaps = [float(p['com']) for p in material]
    if not all(math.isfinite(v) for v in values + gaps):
        return None
    if not 0 < span < 180 or margin < 0 or maximum <= 0:
        return None
    requested = min(gaps) + span + margin
    return min(requested, maximum) if requested > 0 else None


def observed_forward_progress(episode, material):
    """Compare associated observed centers, not the intervening rotor command."""
    current = min((p['com'] for p in material), default=None)
    last = episode.current_recovery_legs[-1] if episode.current_recovery_legs else None
    previous = (last.get('observed_com') if last else
                episode.release_evidence.get('leader_com',
                    episode.release_evidence.get('anchor', {}).get('com')))
    if current is None or previous is None:
        return current, None
    if not math.isfinite(current) or not math.isfinite(previous):
        return current, None
    return current, previous-current


def retained_path(episode, evidence, *, fresh, cfg, gear_ratio):
    """Plan only for the unique original ID, never a spatial replacement.

    The saved exit's far edge is the physical guard. Command count is bounded
    by the existing advance/jitter limits; neither rotor travel nor elapsed
    time establishes piece progress. All other detections remain followers.
    """
    original = [p for p in evidence.get('material', [])
                if episode.leader_id is not None and p['id'] == episode.leader_id]
    if not fresh or len(original) != 1:
        return None
    raw_piece = original[0]
    anchor = episode.release_evidence.get('anchor', {})
    if (not anchor or raw_piece['radius_low'] > anchor.get('radius_high',0)
            or raw_piece['radius_high'] < anchor.get('radius_low',float('inf'))):
        return None  # A same-ID radial jump is not a supported rotor trajectory.
    piece = dict(raw_piece)
    if piece['com'] > 180:
        for key in ('com','low','high'):
            piece[key] -= 360
    original = [piece]
    # Perception folds gaps only inside the exit arc. Unwrap nearby boxes at
    # its far edge into the same coordinates, while distant upstream material
    # keeps its ordinary positive approach distance.
    guard_near = -float(evidence.get('exit_span_deg',0))-cfg.ch3_release_margin_output_deg-UNCERTAINTY_DEG
    reverse_far = max(0,piece['high']+1.5+UNCERTAINTY_DEG)
    followers = []
    for candidate in evidence['material']:
        if candidate is raw_piece:
            continue
        candidate = dict(candidate)
        if (candidate['com'] > 180 and candidate['high']-360 >= guard_near
                and candidate['low']-360 <= reverse_far):
            for key in ('com','low','high'):
                candidate[key] -= 360
        followers.append(candidate)
    current, progress = observed_forward_progress(episode, original)
    jitter_legs = [l for l in episode.recovery_legs if l.get('kind') == 'jitter']
    forwards = [l for l in episode.recovery_legs if l.get('retained') and l.get('kind') != 'jitter']
    last = episode.current_recovery_legs[-1] if episode.current_recovery_legs else None
    amplitude = abs(cfg.jitter_amplitude_motor_deg) / gear_ratio
    clearance = min((p['low'] for p in followers), default=None)
    reverse_clear = (not episode.group_size_unknown
        and all(not _overlap({'low':piece['low'], 'high':piece['high']+1.5+UNCERTAINTY_DEG,
                             'radius_low':piece['radius_low'], 'radius_high':piece['radius_high']}, p)
                for p in followers)
        and all(not (_sectors(p,evidence,-1.5) & set(evidence['drop_sections']))
                for p in evidence['material']))
    target = followthrough_to_exit_end(evidence, {'material':original},
        margin=cfg.ch3_release_margin_output_deg, maximum=abs(cfg.max_move_output_deg))
    jitter_available = (target is not None
        and len(jitter_legs) < min(3, int(cfg.fall_recovery_max_jitter_attempts))
        and 0 < amplitude <= min(1.5,abs(cfg.max_move_output_deg))
        and reverse_clear and (clearance is None or clearance > amplitude))
    no_progress = progress is None or progress <= 2*UNCERTAINTY_DEG
    # At most one orientation-breaking cycle between observed forward legs.
    forward_available = (target is not None and (clearance is None or clearance > 0)
                         and len(forwards) < int(cfg.fast_eject_max_advance_iterations))
    jitter = jitter_available and (no_progress or not forward_available) and (not last or last.get('kind') != 'jitter')
    reason = None if jitter or forward_available else (
        'follower blocks forward travel and the bounded reverse/forward envelope'
        if clearance is not None and clearance <= 0 else
        'no_remaining_guard_path_or_bounded_commands')
    return {'piece':piece, 'followers':followers, 'observed_com':current,
            'observed_progress_deg':progress, 'forward_clearance_deg':clearance,
            'reverse_sweep_clear':reverse_clear, 'jitter':jitter,
            'jitter_available':jitter_available, 'jitter_amplitude_deg':amplitude,
            'forward_degrees':target if forward_available else None,
            'guard_gap_deg':piece['com']+float(evidence.get('exit_span_deg',0)),
            'reason':reason}


def _overlap(a, b):
    return (a['low'] <= b['high'] and b['low'] <= a['high'] and
            a.get('radius_low', 0) <= b.get('radius_high', float('inf')) and
            b.get('radius_low', 0) <= a.get('radius_high', float('inf')))


def _sectors(p, evidence, delta=0.0):
    low, high = p['low']-max(delta, 0), p['high']-min(delta, 0)
    return {(evidence['entry']-evidence['sign']*v) % 360
            for v in range(math.floor(low), math.ceil(high)+1)}


def assess_support(episode, evidence, *, fresh, moving, proposed):
    """Associate an existing material envelope, allowing merges but not lost location.

    A merge expands the same spatially continuous load and permanently marks its
    cardinality unknown. A detached later leader cannot replace a disappeared load.
    """
    material = evidence.get('material', [])
    anchor = episode.support or episode.release_evidence.get('anchor', {})
    travel = episode.support_motion_deg
    corridor = ({**anchor, 'low': anchor['low']-max(travel, 0),
                 'high': anchor['high']-min(travel, 0)} if anchor else {})
    last = episode.current_recovery_legs[-1] if episode.current_recovery_legs else None
    if corridor and last and last.get('kind') == 'jitter' and travel:
        corridor['high'] += abs(travel)  # The owned waveform is symmetric.
    candidates = [p for p in material if corridor and _overlap(p, corridor)]
    owned_ids = {i for i in anchor.get('ids', [episode.leader_id]) if i is not None}
    follower_ids = (set(anchor.get('follower_ids', [])) | {
        p['id'] for p in episode.release_evidence.get('followers', [])}) - {None}
    # A previously detached follower cannot become the leader merely because
    # its padded angular box touches the old leader envelope. Keep it in sweep
    # clearance even when older evidence had already absorbed its ID.
    owned_ids -= follower_ids
    tracked = [p for p in candidates if p['id'] in owned_ids]
    # Commanded travel bounds possible location; it never makes every object
    # in that corridor owned material. Keep detached followers in the sweep.
    # If tracking changed, only one unambiguous spatial candidate can reseed
    # the existing envelope; a known follower cannot replace the leader.
    unassigned = [p for p in candidates if p['id'] not in follower_ids]
    associated = tracked or (unassigned if len(unassigned) == 1 else [])
    # Include touching/overlapping current boxes in the same unknown-size load.
    for _ in material:
        more = [p for p in material if p not in associated and p['id'] not in follower_ids and
                any(_overlap(p, q) for q in associated)]
        if not more:
            break
        associated.extend(more)
    outsiders = [p for p in material if p not in associated]
    # Positive original identity plus continuous spatial support survives leaving
    # the calibrated exit arc. It does not admit a replacement or recover a lost ID.
    retained = (fresh and not episode.location_lost and len(associated) == 1
                and associated[0]['id'] == episode.leader_id
                and associated[0] in tracked)
    supported = bool(associated) and any(
        _sectors(p, evidence) & set(evidence.get('exit_sections', [])) for p in associated)
    ts = evidence.get('ts', 0)
    if fresh and ts > episode.support_last_ts:
        episode.support_last_ts = ts
        if supported or retained:
            episode.support_missing_frames = 0
            old_width = anchor.get('high', 0)-anchor.get('low', 0)
            merged = {'low': min(p['low'] for p in associated),
                      'high': max(p['high'] for p in associated),
                      'radius_low': min(p['radius_low'] for p in associated),
                      'radius_high': max(p['radius_high'] for p in associated),
                      'ids': [p['id'] for p in associated],
                      'follower_ids': list(follower_ids | {p['id'] for p in outsiders})}
            if (len(associated) > 1 or merged['high']-merged['low'] > old_width+2*UNCERTAINTY_DEG
                    or any(_overlap(p, merged) for p in episode.release_evidence.get('followers', []))):
                episode.group_size_unknown = True
            # During a commanded leg retain its original sweep, rather than
            # repeatedly advancing a moving anchor by the full command angle.
            if not moving:
                episode.support = merged
                episode.support_motion_deg = 0.0
        elif not moving:
            episode.support_missing_frames += 1
            if episode.support_missing_frames >= 2:
                episode.location_lost = True
    forward = abs(proposed or 3.0)
    forward_clear = all(p['low'] > forward for p in outsiders)
    reverse_clear = (len(associated) == 1 and not episode.group_size_unknown and
                     all(not _overlap({'low': associated[0]['low'],
                                       'high': associated[0]['high']+1.5+UNCERTAINTY_DEG}, p)
                         for p in outsiders) and
                     all(not (_sectors(p, evidence, -1.5) & set(evidence['drop_sections']))
                         for p in material))
    return {'predicates': {'spatial_association': bool(associated) and not episode.location_lost,
                           'c3_supported_region': supported,
                           'forward_sweep_clear': forward_clear,
                           'reverse_sweep_clear': reverse_clear},
            'same_piece_retained': retained,
            'association_basis': 'continuous angular material envelope',
            'original_id': episode.leader_id,
            'current_ids': [p['id'] for p in associated],
            'material': associated, 'followers': outsiders, 'corridor': corridor,
            'group_size_unknown': episode.group_size_unknown,
            'uncertainty_deg': UNCERTAINTY_DEG,
            'forward_clearance_deg': min((p['low'] for p in outsiders), default=None)}
