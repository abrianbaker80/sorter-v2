"""Original collector crops through generation binding and HTTP serialization."""
from email.parser import BytesParser
from email.policy import default
import hashlib
from types import SimpleNamespace as NS
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
import requests

from recognition_views import capture_release_view
from test_optional_multiview import image, worker


def owned(w, *, views=(), bgr=None):
    return {'episode_id': w.ctx.known_object.transfer_episode_id,
            'piece_uuid': w.ctx.known_object.uuid, 'cycle_id': w.ctx.cycle_id,
            'frame_ts': 99.5, 'bgr': bgr, 'views': views}


@pytest.mark.parametrize('history', ['ready', 'missing', 'stale', 'future',
                                     'wrong_cycle', 'wrong_episode', 'wrong_piece',
                                     'unsupported_transition', 'no_episode'])
def test_optional_history_cannot_block_c4(monkeypatch, history):
    p, w, calls = worker(monkeypatch)
    crop = {'bgr': image(1), 'frame_ts': 99.2, 'source': 'c3_history'}
    if history == 'stale': crop['frame_ts'] = 90
    if history == 'future': crop['frame_ts'] = 99.8
    if history == 'unsupported_transition': crop['source'] = 'transition_fall'
    view = owned(w, views=() if history == 'missing' else (crop,))
    if history == 'wrong_cycle': view['cycle_id'] -= 1
    if history == 'wrong_episode': view['episode_id'] = 'old'
    if history == 'wrong_piece': view['piece_uuid'] = 'old'
    if history == 'no_episode':
        view['episode_id'] = w.ctx.known_object.transfer_episode_id = None
    w.ctx.owned_upstream_view = view
    w.spawnClassifyThread([image(2)])
    w.ctx.classify_thread.join(2)
    assert not w.ctx.classify_thread.is_alive()
    assert len(calls) == 1
    assert len(calls[0]) == 1
    assert w.ctx.classification_error is None
    w._gatherLinkMatches.assert_not_called()
    from defs.events import PauseCommandEvent
    assert not any(isinstance(x, PauseCommandEvent) for x in p._deps[-1].queue)


def test_unproven_history_never_displaces_available_c4(monkeypatch):
    _, w, calls = worker(monkeypatch)
    history = tuple({'bgr': image(i), 'frame_ts': 99.1+i/100,
                     'source': 'c3_history'} for i in [1, 2])
    w.ctx.owned_upstream_view = owned(w, views=history, bgr=image(3))
    w.spawnClassifyThread([image(4), image(5)])
    w.ctx.classify_thread.join(2)
    assert len(calls) == 1 and len(calls[0]) == 2
    assert all(np.array_equal(actual, image(seed))
               for actual, seed in zip(calls[0], [4, 5]))
    assert [r.source for r in w.ctx.known_object.recognition_image_set if r.used].count('c3_history') == 0


@pytest.mark.parametrize('ready', [True, False])
def test_actual_prepared_http_contains_one_combined_request(monkeypatch, ready):
    _, w, _ = worker(monkeypatch)
    from classification.brickognize import _classifyImages
    monkeypatch.setattr('subsystems.classification_channel.simple_state_machine_rev01.base._classifyImages',
                        lambda gc, images, **kw: _classifyImages(None, images, **kw))
    transmitted = []
    def send(session, request, **kwargs):
        # requests has serialized the actual multipart HTTP body at this boundary.
        msg = BytesParser(policy=default).parsebytes(
            b'Content-Type: '+request.headers['Content-Type'].encode()+b'\r\n\r\n'+request.body)
        parts = list(msg.iter_parts())
        assert all(part.get_param('name', header='content-disposition') == 'query_image'
                   for part in parts)
        payloads = [part.get_payload(decode=True) for part in parts]
        transmitted.append(payloads)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"items":[{"id":"3001","score":0.99,"category":"Brick"}],"colors":[]}'
        return response
    monkeypatch.setattr(requests.sessions.Session, 'send', send)
    if ready:
        w.ctx.owned_upstream_view = owned(w, views=(
            {'bgr': image(1), 'frame_ts': 99.2, 'source': 'c3_history'},))
    w.ctx.config.classify_parallel_single_burst = True
    w.spawnClassifyThread([image(2)])
    w.ctx.classify_thread.join(2)
    assert not w.ctx.classify_thread.is_alive()
    assert w.ctx.classification_error is None
    assert len(transmitted) == 1
    payloads = transmitted[0]
    assert len(payloads) == 1
    assert len({hashlib.sha256(p).digest() for p in payloads}) == len(payloads)
    for payload, seed in zip(payloads, [2, 1]):
        decoded = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
        assert np.abs(decoded.astype(float)-image(seed)).mean() < 4


@pytest.mark.parametrize('collector_state', ['ready', 'behind', 'error'])
def test_release_uses_only_ready_exact_collector_history(collector_state):
    leader = {'id': 42, 'bbox': [25, 25, 60, 60]}
    evidence = {'ts': 99.9, 'material': [leader]}
    earlier = image(4)
    lookup = Mock(return_value=[{'frame_ts': 99.7, 'bgr': earlier}]
                  if collector_state == 'ready' else [])
    if collector_state == 'error': lookup.side_effect = RuntimeError('unavailable')
    service = NS(channel_crop_collector=NS(ready_c3_views=lookup),
                 read_pieces_and_frame=lambda _: ([], NS(timestamp=99.9, bgr=image(3))))
    result = capture_release_view(service, evidence, 42, now=100)
    lookup.assert_called_once_with(99.9, 42, (25, 25, 60, 60))
    assert result['evidence'] is evidence and result['bgr'] is not None
    assert len(result['views']) == int(collector_state == 'ready')
    if result['views']:
        assert not np.shares_memory(result['views'][0]['bgr'], earlier)


def test_collected_exact_release_survives_latest_frame_advancing():
    evidence = {'ts': 99.9, 'material': [{'id': 42, 'bbox': [25, 25, 60, 60]}]}
    service = NS(channel_crop_collector=NS(ready_c3_views=lambda *a: [
        {'frame_ts': 99.7, 'bgr': image(4)}]),
        read_pieces_and_frame=lambda _: ([], NS(timestamp=99.95, bgr=image(3))))
    result = capture_release_view(service, evidence, 42, now=100)
    assert result['bgr'] is None and len(result['views']) == 1
