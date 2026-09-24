from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest

from defs.known_object import RecognitionImage
from recognition_views import capture_release_view, distinct_indices
from test_indexed_pocket_pipeline import _pipeline


def image(seed):
    return np.random.default_rng(seed).integers(0, 255, (96, 96, 3), dtype=np.uint8)


def worker(monkeypatch):
    p = _pipeline()
    p._admit(10)
    w = p._tail.payload
    obj = w.ctx.known_object
    obj.transfer_episode_id = 'episode-one'
    w.ctx.captured_crop_timestamps = [99.8, 99.9]
    obj.recognition_image_set = [RecognitionImage(image='', source='c4_burst', ts=t)
                                 for t in w.ctx.captured_crop_timestamps]
    w._selectBurstIndices = lambda captures, n: list(range(len(captures)))
    w._maybeStartHostedColorPredict = lambda *args: None
    w._gatherLinkMatches = Mock(side_effect=AssertionError('must not wait for linking'))
    monkeypatch.setattr('time.time', lambda: 100)
    calls = []
    def provider(gc, images, **kwargs):
        calls.append(images)
        return {'items': [{'id': '3001', 'score': .99}], 'colors': []}
    monkeypatch.setattr('subsystems.classification_channel.simple_state_machine_rev01.base._classifyImages', provider)
    return p, w, calls


@pytest.mark.parametrize('upstream', ['valid', 'missing', 'stale', 'wrong_piece', 'wrong_episode',
                                      'wrong_cycle', 'ambiguous', 'broken', 'grayscale',
                                      'wrong_dtype', 'empty', 'future'])
def test_one_request_ready_only_optional_c3(monkeypatch, upstream):
    p, w, calls = worker(monkeypatch)
    obj = w.ctx.known_object
    view = {'episode_id': obj.transfer_episode_id, 'piece_uuid': obj.uuid,
            'cycle_id': w.ctx.cycle_id, 'frame_ts': 98.5, 'bgr': image(1)}
    if upstream == 'missing': view = None
    elif upstream == 'stale': view['frame_ts'] = 10
    elif upstream == 'wrong_piece': view['piece_uuid'] = 'previous-piece'
    elif upstream == 'wrong_episode': view['episode_id'] = 'previous-episode'
    elif upstream == 'wrong_cycle': view['cycle_id'] -= 1
    elif upstream == 'ambiguous': view = None  # Capture rejects this before binding.
    elif upstream == 'broken': view['bgr'] = None
    elif upstream == 'grayscale': view['bgr'] = image(1)[:, :, 0]
    elif upstream == 'wrong_dtype': view['bgr'] = image(1).astype(np.float64)
    elif upstream == 'empty': view['bgr'] = np.empty((0, 0, 3), dtype=np.uint8)
    elif upstream == 'future': view['frame_ts'] = 101
    w.ctx.owned_upstream_view = view
    w.ctx.config.classify_parallel_single_burst = True  # Legacy setting cannot duplicate calls.
    c4 = image(2)
    w.spawnClassifyThread([c4, c4.copy()])
    w.ctx.classify_thread.join(timeout=2)
    assert not w.ctx.classify_thread.is_alive()
    assert len(calls) == 1 and len(calls[0]) == 1
    assert w.ctx.classification_result['items'][0]['id'] == '3001'
    assert len(w.ctx.classification_attempts) == 1
    assert w.ctx.classification_error is None
    assert len([r for r in obj.recognition_image_set if r.used and r.source == 'c3_transfer']) == 0
    w._gatherLinkMatches.assert_not_called()
    from defs.events import PauseCommandEvent
    assert not any(isinstance(x, PauseCommandEvent) for x in p._deps[-1].queue)


def test_distinct_selection_keeps_fewer_views_not_duplicate_padding():
    first = image(8)
    near = np.clip(first.astype(int)+1, 0, 255).astype(np.uint8)
    images = [first, first.copy(), near, image(9), image(10), image(11), image(12)]
    selected = distinct_indices(images)
    assert selected == [0, 3, 4, 5]


@pytest.mark.parametrize('bad', [None, 'stale', 'different_frame', 'wrong_track', 'ambiguous'])
def test_capture_is_single_piece_fresh_and_paired(bad):
    bgr = image(20)
    leader = {'id': 42, 'bbox': [25, 25, 60, 60]}
    evidence = {'ts': 99.9, 'material': [leader]}
    frame = NS(timestamp=99.9, bgr=bgr)
    if bad == 'stale': frame.timestamp = evidence['ts'] = 90
    elif bad == 'different_frame': frame.timestamp = 99.99
    elif bad == 'ambiguous': evidence['material'].append({'id': 43, 'bbox': [60, 40, 80, 70]})
    result = capture_release_view(NS(read_pieces_and_frame=lambda _: ([], frame)),
                                  evidence, 77 if bad == 'wrong_track' else 42, now=100)
    assert (result is not None) == (bad is None)
    if result is not None:
        assert result['evidence'] is evidence
        assert not np.shares_memory(result['bgr'], bgr)


def test_optional_view_consumed_by_exact_release_and_worker_generation(monkeypatch):
    from test_bounded_transfer import setup_episode
    p, f, clock, e, tick = setup_episode(monkeypatch)
    # A previously prepared image cannot enter a different accepted release.
    p.shared.c3_release_view = {'evidence': {}, 'leader_id': 42, 'bgr': image(1), 'frame_ts': 1100}
    p._episode = None
    p._armArrival(100)
    assert p._upstream_view is None and p.shared.c3_release_view is None
    p.shared.c3_release_view = {'evidence': p.shared.c3_release_evidence,
                               'leader_id': 42, 'bgr': image(1), 'frame_ts': 1100}
    p._episode = None
    p._armArrival(100)
    tick(100.1, True); tick(100.2, True)
    ctx = p._tail.payload.ctx
    view = ctx.owned_upstream_view
    assert view['episode_id'] == p._episode.episode_id
    assert view['piece_uuid'] == ctx.known_object.uuid and view['cycle_id'] == ctx.cycle_id
    ctx.reset()
    assert ctx.owned_upstream_view is None
