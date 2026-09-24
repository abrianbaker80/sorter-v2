"""Optional, transfer-owned views and bounded visual deduplication.

No I/O, waiting, motion, provider requests or ownership mutation lives here.
"""
import time

import cv2
import numpy as np


def capture_release_view(service, evidence, leader_id, *, now=None):
    """Snapshot available original C3 crops plus the paired release crop.

    The collector lookup is memory-only and exact-frame anchored. A collector
    that has not observed the release frame contributes nothing; never wait for
    it or fall back to a broad time-window/model guess.
    """
    now = time.time() if now is None else now
    if leader_id is None or evidence.get('group_size_unknown'):
        return None
    material = evidence.get('material', [])
    leaders = [p for p in material if p.get('id') == leader_id]
    if len(leaders) != 1:
        return None
    ts = float(evidence.get('ts') or 0)
    if not 0 <= now-ts <= 1.5:
        return None
    bbox = tuple(map(int, leaders[0]['bbox']))
    history = []
    try:
        collector = getattr(service, 'channel_crop_collector', None)
        if collector is not None:
            for view in collector.ready_c3_views(ts, leader_id, bbox)[:4]:
                stamp, bgr = view.get('frame_ts', 0), view.get('bgr')
                if 0 <= ts-stamp <= 1.5 and valid_bgr(bgr):
                    history.append({'frame_ts': stamp, 'bgr': bgr.copy(),
                                    'source': 'c3_history'})
    except Exception:
        history = []  # The original live release view remains optional too.
    envelope = {'frame_ts': ts, 'leader_id': leader_id, 'evidence': evidence,
                'views': tuple(history), 'bgr': None}
    sample = service.read_pieces_and_frame(3)
    if sample is None:
        return envelope if history else None
    _, frame = sample
    frame_ts = float(getattr(frame, 'timestamp', 0) or 0)
    bgr = getattr(frame, 'bgr', None)
    if frame_ts != ts or not valid_bgr(bgr):
        return envelope if history else None
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = bbox
    if not 0 <= x1 < x2 <= w or not 0 <= y1 < y2 <= h:
        return envelope if history else None
    box = (max(0, x1-15), max(0, y1-15), min(w, x2+15), min(h, y2+15))
    if any(p is not leaders[0] and
           max(box[0], p['bbox'][0]) < min(box[2], p['bbox'][2]) and
           max(box[1], p['bbox'][1]) < min(box[3], p['bbox'][3]) for p in material):
        return None
    crop = bgr[box[1]:box[3], box[0]:box[2]].copy()
    correct = getattr(frame, "correct_pixels", None)
    envelope['bgr'] = correct(crop, "recognition_crop_color_ms") if correct is not None else crop
    return envelope


def valid_bgr(bgr):
    return (isinstance(bgr, np.ndarray) and bgr.dtype == np.uint8 and
            bgr.ndim == 3 and bgr.shape[2] == 3 and bool(bgr.size))


def ready_image_views(view, *, episode_id, piece_uuid, cycle_id, now, max_age):
    """Already copied images only; validate the envelope and every image age."""
    view = ready_owned_view(view, episode_id=episode_id, piece_uuid=piece_uuid,
                           cycle_id=cycle_id, now=now, max_age=max_age)
    if view is None:
        return []
    candidates = list(view.get('views') or ())[:4]
    candidates.append({'bgr': view.get('bgr'), 'frame_ts': view['frame_ts'],
                       'source': 'c3_transfer'})
    ready = []
    for image in candidates:
        if not isinstance(image, dict):
            continue
        stamp = image.get('frame_ts', float('-inf'))
        if (image.get('source') in ('c3_history', 'c3_transfer') and
                0 <= view['frame_ts']-stamp <= 1.5 and
                0 <= now-stamp <= max_age and valid_bgr(image.get('bgr'))):
            ready.append(image)
    return [ready[i] for i in distinct_indices([r['bgr'] for r in ready], limit=2)]


def ready_owned_view(view, *, episode_id, piece_uuid, cycle_id, now, max_age):
    if not isinstance(view, dict) or not episode_id or not piece_uuid or cycle_id is None:
        return None
    if (view.get('episode_id'), view.get('piece_uuid'), view.get('cycle_id')) != (
            episode_id, piece_uuid, cycle_id):
        return None
    if not 0 <= now-view.get('frame_ts', float('-inf')) <= max_age:
        return None
    return view


def distinct_indices(images, *, limit=4):
    """Keep ordered preferred views, removing near-identical crop fingerprints."""
    selected, signatures = [], []
    for i, bgr in enumerate(images):
        if bgr is None or not getattr(bgr, 'size', 0):
            continue
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        thumb = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
        coefficients = cv2.dct(thumb)[:8, :8].flatten()[1:]
        fingerprint = coefficients > np.median(coefficients)
        aspect = bgr.shape[1]/bgr.shape[0]
        if any(np.count_nonzero(fingerprint != old) <= 6 and
               abs(aspect/ratio-1) <= .12 for old, ratio in signatures):
            continue
        selected.append(i)
        signatures.append((fingerprint, aspect))
        if len(selected) >= limit:
            break
    return selected
