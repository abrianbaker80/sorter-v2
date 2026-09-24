"""Passive diagnostics only. No hardware, controller or ownership writes."""
import json
import math
import queue
import threading
import time
from collections import deque
from pathlib import Path

_events = queue.Queue(maxsize=64)
_lost_events = 0


def emit(kind, receipt, wall, steps=0, degrees=0.0):
    """Best-effort nonblocking metadata enqueue; never called for control."""
    global _lost_events
    try:
        _events.put_nowait((kind, receipt, wall, steps, degrees, _lost_events))
    except Exception:
        _lost_events += 1


def extract(image, center):
    import cv2
    import numpy as np
    h, w = image.shape[:2]
    scale = 960.0 / w
    small = cv2.resize(image, (960, round(h * scale))) if w != 960 else image
    cx, cy = center[0] * scale, center[1] * scale
    yy, xx = np.indices(small.shape[:2])
    rr = np.hypot(xx - cx, yy - cy)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    # Rotor hub annulus measured in the existing 960-wide view, not LEGO ROIs.
    mask = ((hsv[:, :, 0] > 85) & (hsv[:, :, 0] < 115)
            & (hsv[:, :, 1] > 95) & (rr > 45) & (rr < 115)).astype('uint8')
    count, labels, stats, centers = cv2.connectedComponentsWithStats(mask)
    components = []
    for j in range(1, count):
        if stats[j, 4] < 12:
            continue
        x, y = centers[j]
        radii = rr[labels == j]
        components.append((math.degrees(math.atan2(y-cy, x-cx)) % 360,
                           int(stats[j, 4]), float(radii.min()), float(radii.max())))
    components.sort()
    groups = []
    for component in components:
        if groups and component[0] - groups[-1][-1][0] < 3:
            groups[-1].append(component)
        else:
            groups.append([component])
    if len(groups) > 1 and (groups[0][0][0] + 360 - groups[-1][-1][0]) < 3:
        groups[0] += groups.pop()
    if not 8 <= len(groups) <= 12:
        return None
    angles, dashed, quality = [], [], []
    for group in groups:
        theta = math.degrees(math.atan2(
            sum(c[1]*math.sin(math.radians(c[0])) for c in group),
            sum(c[1]*math.cos(math.radians(c[0])) for c in group))) % 360
        angles.append(theta)
        radial = sorted(group, key=lambda c: c[2])
        if len(radial) == 2 and radial[1][2] - radial[0][3] > 8:
            dashed.append(theta)
        elif len(radial) != 1 or radial[0][3]-radial[0][2] < 20:
            return None
        quality.append({'angle': theta, 'components': len(group),
                        'area_px': sum(c[1] for c in group),
                        'radial_span_px': max(c[3] for c in group)-min(c[2] for c in group),
                        'component_angle_spread_deg': max(abs((c[0]-theta+180)%360-180) for c in group)})
    angles.sort()
    if len(dashed) > 1:
        return None
    if dashed:
        # Anchor identities to the unique dashed spoke. Off-grid extras are
        # ignored; duplicate plausible identities remain ambiguous.
        ids = {}
        extra = []
        for angle in angles:
            index = round(((angle-dashed[0]) % 360)/36) % 10
            error = abs((angle-dashed[0]-36*index+180) % 360-180)
            if error > 3:
                extra.append(angle)
                continue
            if index in ids:
                return None
            ids[index] = angle
        if len(ids) < 8 or len(extra) > 2:
            return None
        angles = sorted(ids.values())
    else:
        ids = {}
        extra = []
        if len(angles) != 10:
            return None
    gaps = [(angles[(i+1) % len(angles)]-angles[i]) % 360 for i in range(len(angles))]
    return {'angles': angles, 'dashed': dashed[0] if dashed else None,
            'spacing': gaps, 'quality': quality, 'extra_angles': extra,
            'missing_indices': sorted(set(range(10))-set(ids)) if dashed else [],
            'phase_mod36_degrees': circular_median([x % 36 for x in angles], 36)}


def circular_median(values, period=360):
    import statistics
    anchor = values[0]
    return (anchor + statistics.median([(v-anchor+period/2) % period-period/2 for v in values])) % period


def identities(observation):
    if observation is None or observation.get('dashed') is None:
        return None
    result = {}
    for angle in observation['angles']:
        index = round(((angle-observation['dashed']) % 360)/36) % 10
        if abs((angle-observation['dashed']-index*36+180) % 360-180) > 3:
            continue
        if index in result:
            return None
        result[index] = angle
    return result if 0 in result else None


def compare(pre, post, commanded):
    import statistics
    if not math.isfinite(commanded) or abs(commanded) >= 360:
        return None
    first, second = identities(pre), identities(post)
    if first is None or second is None:
        return None
    delta = (post['dashed']-pre['dashed']) % 360
    votes = {i: delta + ((second[i]-first[i]-delta+180) % 360-180)
             for i in first.keys() & second.keys() if i != 0}
    # Seven independent solid identities support two missing/noisy solids.
    if len(votes) < 7:
        return None
    fitted = statistics.median(votes.values())
    inliers = {i:v for i,v in votes.items() if abs(v-fitted) <= 3}
    if len(inliers) < 7:
        return None
    fitted = statistics.median(inliers.values())
    errors = {i:v-fitted for i,v in inliers.items()}
    if max(abs(x) for x in errors.values()) > 3 or abs(fitted-delta) > 3:
        return None
    # Resolve only the full-turn branch, never a 36-degree alias. Direction
    # and magnitude do not change the fitted fractional rotation.
    short = (fitted+180) % 360-180
    candidates = sorted((short-360, short, short+360), key=lambda a: abs(a-commanded))
    if abs(candidates[1]-commanded)-abs(candidates[0]-commanded) < 5:
        return None
    observed = candidates[0]
    return {'observed_degrees': observed, 'residual_degrees': commanded-observed,
            'dashed_visible': True, 'dashed_delta_degrees': delta,
            'dashed_fit_error_degrees': delta-fitted,
            'solid_votes_degrees': votes, 'solid_fit_errors_degrees': errors,
            'solid_inliers': len(inliers), 'solid_candidates': len(votes),
            'max_solid_fit_error_degrees': max(abs(x) for x in errors.values()),
            'quality': 'unique_dashed_anchor_and_at_least_seven_solid_inliers'}


def start(service, logger, irl_config):
    from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter
    degrees_per_step = C4FiveSectorPlatter.from_irl_config(irl_config).motor_microsteps_to_output_degrees(1)
    def run():
        import cv2
        frames = deque(maxlen=8)
        pending = {}
        completed = deque(maxlen=128)
        root = Path('c4_marker_telemetry')
        root.mkdir(exist_ok=True)
        serial = 0
        last_ts = 0.0
        while True:
            try:
                pair = service.read_pieces_and_frame(4)
                center = service.channel_center(4)
                if pair is not None and center is not None:
                    frame = pair[1]
                    if frame.timestamp > last_ts:
                        last_ts = frame.timestamp
                        try:
                            markers = extract(frame.bgr, center)
                        except Exception:
                            markers = None
                        frames.append((frame.timestamp, markers, frame.bgr))
                while True:
                    try:
                        kind, receipt, wall, steps, degrees, loss_epoch = _events.get_nowait()
                    except queue.Empty:
                        break
                    if kind == 'boundary':
                        for old in pending.values():
                            old.setdefault('next_start_wall', wall)
                        continue
                    key = (receipt.motor_id, receipt.generation)
                    if kind == 'start':
                        degrees = steps * degrees_per_step
                        for old in pending.values():
                            old.setdefault('next_start_wall', wall)
                        pre = next((f for f in reversed(frames) if 0 <= wall-f[0] <= .3), None)
                        pending[key] = dict(pre=pre, start_wall=wall, steps=steps,
                                            commanded_degrees=degrees, receipt=receipt,
                                            lost_events=loss_epoch)
                    elif key in pending and key not in completed:
                        pending[key].setdefault('completion_wall', wall)
                for key, item in list(pending.items()):
                    end = item.get('completion_wall')
                    post = select_post(frames, end, item.get('next_start_wall', float('inf')))
                    deadline = min(end+1 if end is not None else item['start_wall']+15,
                                   item.get('next_start_wall', float('inf')))
                    if post is None and time.time() < deadline:
                        continue
                    pre = item['pre']
                    receipt = item['receipt']
                    result = compare(pre[1] if pre else None, post[1] if post else None,
                                     item['commanded_degrees'])
                    if item['lost_events'] != _lost_events:
                        result = None
                    row = dict({k:v for k,v in item.items() if k not in ('pre','receipt')}, motor_id=key[0], generation=key[1],
                               start_position=receipt.start_position, target_position=receipt.target_position,
                               marker_measurement='available' if result else 'unavailable',
                               pre_retrieval_wall=pre[0] if pre else None,
                               post_retrieval_wall=post[0] if post else None,
                               pre_markers=pre[1] if pre else None, post_markers=post[1] if post else None,
                               **(result or {}))
                    # Fixed-size evidence ring; source-resolution frames are
                    # encoded only here, never on a motion or inference thread.
                    slot = serial % 64
                    row['evidence_slot'] = slot
                    serial += 1
                    completed.append(key)
                    del pending[key]
                    for name, f in [('pre', pre), ('post', post)]:
                        if f is not None:
                            cv2.imwrite(str(root / f'{slot}-{name}.jpg'), f[2])
                    (root / f'{slot}.json').write_text(json.dumps(row), encoding='utf8')
                    logger.info('[C4-MARKER] ' + json.dumps(row))
                if len(pending) > 64:
                    pending.clear()
            except Exception:
                # Diagnostic failure cannot propagate to machine execution.
                pass
            time.sleep(.1)
    threading.Thread(target=run, name='c4-marker-telemetry', daemon=True).start()


def select_post(frames, completion, next_start=float('inf')):
    if completion is None:
        return None
    return next((f for f in frames if completion < f[0] <= completion+1 and f[0] < next_start), None)
