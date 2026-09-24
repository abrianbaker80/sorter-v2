"""Receiver generations are not independent cross-camera piece identities."""
from unittest.mock import Mock

import numpy as np
import pytest

from defs.events import PauseCommandEvent
from test_optional_multiview import image, worker


@pytest.mark.parametrize('uid,episode,leader,stamp', [
    ('0fd0896f-4ed0-4dea-a4a5-eff263a0b4f5', '7f5929e1b22643e7bd653a9377497389', 16, 1789770629.4210868),
    ('04542377-7055-48be-8d17-837d226e7157', 'ea81dcb0d176455683029e7b5fd416ef', 46, 1789770635.2884073),
    ('d8ea32e6-e197-4392-9520-c68d31bbb28d', '1a1f5c16a5de44a8952303501e76bdb9', 60, 1789770653.4867814),
])
def test_observed_cross_piece_envelope_cannot_enter_provider(monkeypatch, uid, episode, leader, stamp):
    p, w, calls = worker(monkeypatch)
    obj = w.ctx.known_object
    obj.uuid, obj.transfer_episode_id = uid, episode
    assert obj.tracked_global_id is None
    monkeypatch.setattr('time.time', lambda: stamp + 2)
    # Same receiving UUID, episode and worker cycle as the observed failures.
    # These labels were assigned after arrival, not observed on both pieces.
    w.ctx.owned_upstream_view = {
        'episode_id': episode, 'piece_uuid': uid, 'cycle_id': w.ctx.cycle_id,
        'leader_id': leader, 'frame_ts': stamp, 'bgr': image(1), 'views': (),
    }
    c4 = image(2)
    w.spawnClassifyThread([c4])
    w.ctx.classify_thread.join(2)
    assert not w.ctx.classify_thread.is_alive()
    assert len(calls) == 1 and len(calls[0]) == 1
    assert np.array_equal(calls[0][0], c4)
    assert w.ctx.owned_upstream_view is None
    assert len(w.ctx.classification_attempts) == 1
    assert w.ctx.classification_error is None
    assert all(r.source == 'c4_burst' for r in obj.recognition_image_set)
    assert not any(isinstance(e, PauseCommandEvent) for e in p._deps[-1].queue)


@pytest.mark.parametrize('candidate', [
    'same_receiving_generation', 'previous_generation', 'next_generation',
    'nearest_time_wrong_piece', 'stale_history', 'multiple_candidates',
    'locally_valid_history', 'missing_history',
])
def test_uncertain_upstream_never_waits_or_leaks(monkeypatch, candidate):
    p, w, calls = worker(monkeypatch)
    obj = w.ctx.known_object
    generation = w.ctx.cycle_id
    view = {'episode_id': obj.transfer_episode_id, 'piece_uuid': obj.uuid,
            'cycle_id': generation, 'frame_ts': 99.9, 'bgr': image(1),
            'views': ({'source': 'c3_history', 'frame_ts': 99.8, 'bgr': image(3)},)}
    if candidate == 'previous_generation': view['cycle_id'] -= 1
    if candidate == 'next_generation': view['cycle_id'] += 1
    if candidate == 'nearest_time_wrong_piece': view['piece_uuid'] = 'other-piece'
    if candidate == 'stale_history': view['views'][0]['frame_ts'] = 1
    if candidate == 'multiple_candidates':
        view['views'] += ({'source': 'c3_history', 'frame_ts': 99.9, 'bgr': image(4)},)
    if candidate == 'missing_history': view = None
    w.ctx.owned_upstream_view = view
    w.encodeFrame = Mock(side_effect=AssertionError('must not encode unproven upstream image'))
    w.spawnClassifyThread([image(2)])
    w.ctx.classify_thread.join(2)
    assert not w.ctx.classify_thread.is_alive()
    assert len(calls) == 1 and len(calls[0]) == 1
    assert w.ctx.classification_error is None
    assert w.ctx.owned_upstream_view is None
    w._gatherLinkMatches.assert_not_called()
    w.encodeFrame.assert_not_called()
    assert not any(isinstance(e, PauseCommandEvent) for e in p._deps[-1].queue)
