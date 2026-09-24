from types import SimpleNamespace

import numpy as np
import pytest

import perception.channel_crop_capture as capture


def piece(tid=7, bbox=(20, 20, 45, 45), deg=20):
    return SimpleNamespace(sv_bt_track_id=tid, bbox=bbox,
                           com_forward_to_exit_deg=deg, com_section=20, zone_code=3)


class Harness:
    def __init__(self, monkeypatch):
        self.now = 1000.0
        self.worker = SimpleNamespace(_tracker=SimpleNamespace(_tracker=object()))
        self.sample = None
        self.service = SimpleNamespace(
            workers=lambda: {3: self.worker},
            read_pieces_and_frame=lambda _channel: self.sample,
        )
        self.collector = capture.ChannelCropCollector(perception_service=self.service)
        self.writes = []
        monkeypatch.setattr(capture.channel_crop_store, 'enqueue',
                            lambda jpeg, meta: self.writes.append((jpeg, meta)))
        monkeypatch.setattr(capture.time, 'time', lambda: self.now)

    def tick(self, ts, pieces):
        self.now = ts
        frame = SimpleNamespace(timestamp=ts,
                                bgr=np.full((120, 160, 3), int(ts*10) % 255, np.uint8))
        self.sample = (pieces, frame)
        self.collector._collectChannel(3, self.collector._cfg)

    def ready(self, ts, tid=7, bbox=(20, 20, 45, 45)):
        return self.collector.ready_c3_views(ts, tid, bbox)

    def populated(self):
        # Initial publication may predate tracker incarnation observation.
        self.tick(1000.0, [piece(deg=24)])
        self.tick(1000.1, [piece(deg=20)])
        self.tick(1000.2, [piece(deg=16)])


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


def test_ready_returns_original_crops_from_exact_continuous_release(h):
    h.populated()
    h.tick(1000.3, [piece(deg=12)])
    result = h.ready(1000.3)
    assert [r['frame_ts'] for r in result] == [1000.1, 1000.2, 1000.3]
    assert all(r['source'] == 'c3_history' for r in result)
    assert all(r['bgr'].shape == (37, 37, 3) for r in result)
    assert all(not r['bgr'].flags.writeable for r in result)
    # Original capture cadence, JPEG persistence and metadata remain intact.
    assert len(h.writes) == 4
    assert [meta['ts'] for _, meta in h.writes] == [1000., 1000.1, 1000.2, 1000.3]
    assert all(meta['channel'] == 3 and meta['track_id'] == 7 for _, meta in h.writes)
    assert all(jpeg.startswith(b'\xff\xd8') for jpeg, _ in h.writes)


@pytest.mark.parametrize('ts,tid,bbox', [
    (1000.21, 7, (20, 20, 45, 45)),
    (1000.2, 8, (20, 20, 45, 45)),
    (1000.2, 7, (21, 20, 45, 45)),
])
def test_lookup_requires_exact_observation_anchor(h, ts, tid, bbox):
    h.populated()
    assert h.ready(ts, tid, bbox) == []


def test_empty_frame_invalidates_and_reused_id_cannot_reuse_old_crops(h):
    h.populated()
    h.tick(1000.3, [])
    assert h.ready(1000.2) == []
    h.tick(1000.4, [piece(deg=12)])
    assert [v['frame_ts'] for v in h.ready(1000.4)] == [1000.4]
    assert h.ready(1000.2) == []


@pytest.mark.parametrize('neighbors', [
    [piece(8, (90, 20, 115, 45))],  # Original track absent, scene not empty.
    [piece(), piece(7, (90, 20, 115, 45))],  # Duplicate ID.
    [piece(), piece(None, (44, 20, 60, 45))],  # Untracked overlapping neighbor.
    [piece(), piece(8, (48, 20, 65, 45))],  # Neighbor enters padded crop only.
])
def test_ambiguous_or_absent_observations_invalidate_optional_history(h, neighbors):
    h.populated()
    h.tick(1000.3, neighbors)
    assert h.ready(1000.2) == []
    assert h.ready(1000.3) == []


def test_no_id_still_captures_to_disk_but_never_enriches(h):
    h.tick(1000.0, [piece(None, deg=24)])
    h.tick(1000.1, [piece(None, deg=20)])
    assert len(h.writes) == 2
    assert h.ready(1000.1, None) == []


def test_observation_gap_starts_new_optional_generation(h):
    h.populated()
    h.tick(1001.8, [piece(deg=12)])
    assert h.ready(1000.2) == []
    assert [v['frame_ts'] for v in h.ready(1001.8)] == [1001.8]


def test_slower_inference_preserves_continuous_fresh_crops(h):
    h.tick(1000.0, [piece(deg=24)])
    h.tick(1000.4, [piece(deg=20)])
    h.tick(1000.8, [piece(deg=16)])
    assert [v['frame_ts'] for v in h.ready(1000.8)] == [1000.4, 1000.8]


def test_timestamp_rewind_invalidates_and_requires_newer_sample(h):
    h.populated()
    h.tick(999.9, [piece(deg=12)])
    assert h.ready(1000.2) == []
    assert h.ready(999.9) == []


@pytest.mark.parametrize('replace_worker', [False, True])
def test_tracker_incarnation_change_rejects_before_tick_and_skips_old_sample(h, replace_worker):
    h.populated()
    if replace_worker:
        h.worker = SimpleNamespace(_tracker=h.worker._tracker)
    else:
        h.worker._tracker._tracker = object()
    assert h.ready(1000.2) == []  # Reader sees replacement before collector does.
    h.tick(1000.2, [piece(deg=16)])
    assert h.ready(1000.2) == []  # May still be a pre-replacement publication.
    h.tick(1000.3, [piece(deg=12)])
    assert [v['frame_ts'] for v in h.ready(1000.3)] == [1000.3]


def test_missing_tracker_or_worker_introspection_never_waits(h, monkeypatch):
    h.populated()
    h.worker._tracker._tracker = None
    assert h.ready(1000.2) == []
    monkeypatch.setattr(h.service, 'workers', lambda: (_ for _ in ()).throw(RuntimeError('busy')))
    assert h.ready(1000.2) == []


def test_ready_method_has_no_crop_disk_or_perception_wait(h, monkeypatch):
    h.populated()
    def forbidden(*_args, **_kwargs):
        raise AssertionError('optional read must consume only published ready pixels')
    monkeypatch.setattr(h.service, 'read_pieces_and_frame', forbidden)
    monkeypatch.setattr(capture.channel_crop_store, 'getCropFileById', forbidden)
    monkeypatch.setattr(capture.channel_crop_store, 'listCropsByTimeRange', forbidden)
    monkeypatch.setattr(capture.cv2, 'imencode', forbidden)
    monkeypatch.setattr(capture.time, 'sleep', forbidden)
    assert len(h.ready(1000.2)) == 2


def test_busy_encoding_cannot_keep_now_ambiguous_history_available(h, monkeypatch):
    h.populated()
    original = h.collector._considerPiece
    def inspect_during_encode(*args, **kwargs):
        assert h.ready(1000.2) == []
        return original(*args, **kwargs)
    monkeypatch.setattr(h.collector, '_considerPiece', inspect_during_encode)
    h.tick(1000.3, [piece(), piece(None, (42, 20, 65, 45))])


def test_crops_older_than_capture_budget_expire_without_new_capture(h):
    h.populated()
    for index in range(3, 20):
        h.tick(1000.0+index/10, [piece(deg=16)])
    assert h.ready(1001.9) == []


def test_stop_discards_optional_history(h):
    h.populated()
    h.collector.stop()
    assert h.ready(1000.2) == []


def test_optional_cache_is_bounded(h):
    # Cadence continues unchanged, including no additional crops at rest.
    h.populated()
    for index in range(3, 45):
        h.tick(1000.0+index/10, [piece(deg=16)])
    assert len(h.collector._ready_observations) <= 32
    assert all(len(run.crops) <= 4 for run in h.collector._ready_runs.values())
    assert len(h.writes) == 3


def test_non_sorting_loop_discards_optional_history(h, monkeypatch):
    h.populated()
    monkeypatch.setattr(h.collector, '_isSorting', lambda: False)
    monkeypatch.setattr(h.collector._stop, 'wait', lambda _seconds: h.collector._stop.set())
    h.collector._loop()
    assert h.ready(1000.2) == []


def test_optional_track_cap_does_not_limit_ordinary_disk_capture(h):
    def crowded(deg):
        return [piece(i+1, (8+i%7*20, 8+i//7*20,
                            14+i%7*20, 14+i//7*20), deg)
                for i in range(35)]
    h.tick(1000.0, crowded(24))
    h.tick(1000.1, crowded(20))
    assert len(h.collector._ready_runs) == 32
    assert len(h.collector._ready_observations) == 32
    assert len(h.writes) == 70


def test_inflight_collection_cannot_republish_after_stop(h, monkeypatch):
    h.populated()
    original = h.collector._considerPiece
    def stop_during_encode(*args, **kwargs):
        result = original(*args, **kwargs)
        h.collector._stop.set()
        return result
    monkeypatch.setattr(h.collector, '_considerPiece', stop_during_encode)
    h.tick(1000.3, [piece(deg=12)])
    assert h.ready(1000.3) == []
    assert h.ready(1000.2) == []
