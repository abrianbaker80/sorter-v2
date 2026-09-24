"""Local evidence replay only. No machine, provider network or physical owner."""

from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
import threading

import cv2
import numpy as np
import pytest
import requests

from defs.known_object import KnownObject
from recognition_journey import (
    Detection,
    Observation,
    RecognitionJourneys,
    Scene,
    Stage,
    TrackAlias,
    select_views,
)
from recognition_journey_capture import (
    HistoryReference,
    brickognize_provider,
    retain_ready_history,
    retain_rolling_fall,
    retain_track_history,
)
from vision.tracking.drop_zone_burst import RollingFrameBuffer


BOX = (20, 20, 100, 100)
REGION = (0, 0, 120, 120)
A = TrackAlias(3, "c3-camera1-tracker1", 42, 7)
B = TrackAlias(4, "c4-camera1-tracker1", 105, 2)
UUID = "7272c5aa-41e9-4e98-86b1-392991d33738"


def pose(seed):
    # Synthetic distinct views; not presented as historical physical attribution.
    rng = np.random.default_rng(seed)
    image = np.full((80, 80, 3), 170, np.uint8)
    image[12:66, 12:66] = rng.integers(15, 235, (54, 54, 3), dtype=np.uint8)
    return image


def scene(alias=A, seq=1, ts=100.0, seed=1, detections=None):
    bgr = np.full((140, 160, 3), 170, np.uint8)
    bgr[20:100, 20:100] = pose(seed)
    return Scene(
        alias.channel,
        alias.epoch,
        seq,
        ts,
        bgr,
        (Detection(alias, BOX),) if detections is None else tuple(detections),
    )


class Replay:
    def __init__(self):
        self.owner = RecognitionJourneys()
        self.owner.set_epoch(3, A.epoch)
        self.owner.set_epoch(4, B.epoch)
        self.piece = KnownObject(uuid=UUID)
        self.j = self.owner.start_c3(
            self.piece, episode_id="transfer-71", generation=12, scene=scene(), alias=A
        )

    def crossing(self):
        self.release = scene(seq=2, ts=100.2, seed=2)
        self.empty = scene(B, seq=10, ts=100.1, detections=[])
        return self.owner.begin_crossing(
            self.j,
            release=self.release,
            empty_c4=self.empty,
            c3_region=REGION,
            c4_region=REGION,
        )

    def land(self):
        return self.owner.land(
            self.j,
            scene=scene(B, seq=14, ts=100.6, seed=4),
            c3_after=scene(seq=3, ts=100.65, detections=[]),
        )

    def settled(self, alias=B):
        return self.owner.observe(
            self.j, scene(alias, seq=15, ts=100.8, seed=5), alias, Stage.SETTLED
        )


def test_starts_with_original_knownobject_on_c3_and_exit_attaches():
    r = Replay()
    assert r.j.piece is r.piece and r.j.uuid == UUID
    assert r.j.c3_alias == A and len(r.j.aliases) == 1
    assert [o.stage for o in r.j.observations] == [Stage.C3_READY]
    assert r.crossing()
    assert [o.stage for o in r.j.observations] == [Stage.C3_READY, Stage.C3_EXIT]
    assert r.owner.prepare_request(r.j) is None


def test_original_ready_collector_history_retained_at_exact_c3_anchor(monkeypatch):
    from test_ready_channel_crops import Harness, piece

    h = Harness(monkeypatch)
    alias = replace(A, track_id=7)
    proofs = {}
    for seq, stamp in enumerate((1000.0, 1000.1, 1000.2, 1000.3), 17):
        h.tick(stamp, [piece(deg=24 - (seq - 17) * 4)])
        proofs[stamp] = Scene(
            3,
            A.epoch,
            seq,
            stamp,
            h.sample[1].bgr.copy(),
            (Detection(alias, (20, 20, 45, 45)),),
        )
    h.service.channel_crop_collector = h.collector
    r = Replay()
    s = Scene(
        3, A.epoch, 20, 1000.3, h.sample[1].bgr, (Detection(alias, (20, 20, 45, 45)),)
    )
    j = r.owner.start_c3(
        KnownObject(), episode_id="new-transfer", generation=13, scene=s, alias=alias
    )
    evidence = {"ts": 1000.3, "material": [{"id": 7, "bbox": (20, 20, 45, 45)}]}
    assert (
        retain_ready_history(
            r.owner, j, h.service, evidence, s, lambda image: proofs[image["frame_ts"]]
        )
        == 3
    )
    assert [o.timestamp for o in j.observations if o.stage == Stage.C3_HISTORY] == [
        1000.1,
        1000.2,
        1000.3,
    ]
    assert all(not o.bgr.flags.writeable for o in j.observations)
    h.worker._tracker._tracker = object()
    assert (
        retain_ready_history(
            r.owner, j, h.service, evidence, s, lambda image: proofs[image["frame_ts"]]
        )
        == 0
    )


def full_replay():
    r = Replay()
    assert r.crossing()
    airborne = replace(B, track_id=104)
    fall = scene(airborne, seq=12, ts=100.4, seed=3)
    buffer = RollingFrameBuffer()
    # Previous piece's pre-trigger frame must never be carried forward.
    buffer.push(scene(B, seq=9, ts=99.9).bgr, 99.9)
    buffer.push(fall.bgr, fall.timestamp)
    bridge = scene(
        B,
        seq=13,
        ts=100.5,
        seed=3,
        detections=[Detection(airborne, BOX), Detection(B, BOX)],
    )
    buffer.push(bridge.bgr, bridge.timestamp)
    proofs = {fall.timestamp: fall, bridge.timestamp: bridge}
    assert (
        retain_rolling_fall(r.owner, buffer, lambda frame: proofs.get(frame.timestamp))
        == 2
    )
    assert r.land()
    assert r.settled()
    return r


def test_reconstructed_c3_fall_new_c4_track_one_journey_and_diverse_request():
    r = full_replay()
    assert r.j.uuid == UUID
    assert {a.track_id for a in r.j.aliases} == {42, 104, 105}
    assert {o.stage for o in r.j.observations} == {
        Stage.C3_READY,
        Stage.C3_EXIT,
        Stage.FALL,
        Stage.LANDING,
        Stage.SETTLED,
    }
    request = r.owner.prepare_request(r.j)
    assert len(request.images) == 4 and request.piece_uuid == UUID
    assert {o.stage for o in request.images} >= {
        Stage.FALL,
        Stage.LANDING,
        Stage.SETTLED,
    }
    assert any(o.stage in (Stage.C3_READY, Stage.C3_EXIT) for o in request.images)
    assert request.episode_id == "transfer-71" and request.generation == 12


def test_explicit_same_frame_detection_bridges_multiple_c4_aliases():
    r = full_replay()
    new = replace(B, track_id=106)
    bridge = scene(
        B, seq=16, ts=100.9, detections=[Detection(B, BOX), Detection(new, BOX)]
    )
    assert r.owner.bind_same_detection(r.j, old_alias=B, new_alias=new, scene=bridge)
    assert r.owner.observe(r.j, scene(new, seq=17, ts=101, seed=6), new, Stage.SETTLED)
    assert r.j.uuid == UUID and new in r.j.aliases


@pytest.mark.parametrize(
    "kind", ["nearby", "absent_old", "same_id_reused", "wrong_epoch", "third_piece"]
)
def test_ambiguous_reassociation_omits_image_without_losing_valid_views(kind):
    r = full_replay()
    new = replace(B, track_id=106)
    detections = [Detection(B, BOX), Detection(new, BOX)]
    if kind == "nearby":
        detections[1] = Detection(new, (21, 20, 101, 100))
    if kind == "absent_old":
        detections = [Detection(new, BOX)]
    if kind == "same_id_reused":
        new = replace(B, generation=3)
        detections[1] = Detection(new, BOX)
    if kind == "wrong_epoch":
        new = replace(new, epoch="other")
        detections[1] = Detection(new, BOX)
    if kind == "third_piece":
        detections.append(Detection(replace(B, track_id=200), BOX))
    before = len(r.j.observations)
    assert not r.owner.bind_same_detection(
        r.j,
        old_alias=B,
        new_alias=new,
        scene=scene(B, seq=16, ts=100.9, detections=detections),
    )
    assert not r.owner.observe(r.j, scene(new, seq=17, ts=101), new, Stage.SETTLED)
    assert len(r.j.observations) == before and r.owner.prepare_request(r.j)


@pytest.mark.parametrize(
    "bad", ["follower", "previous", "generation", "epoch", "stale"]
)
def test_foreign_c3_or_c4_alias_and_stale_frames_cannot_contaminate(bad):
    r = full_replay()
    alias = B
    if bad == "follower":
        alias = replace(A, track_id=43)
    if bad == "previous":
        alias = replace(B, track_id=99)
    if bad == "generation":
        alias = replace(B, generation=3)
    if bad == "epoch":
        alias = replace(B, epoch="restarted")
    s = scene(alias, seq=1 if bad == "stale" else 20, ts=99 if bad == "stale" else 101)
    assert not r.owner.observe(
        r.j, s, alias, Stage.C3_EXIT if alias.channel == 3 else Stage.SETTLED
    )


@pytest.mark.parametrize("ts", [99, 100.2, 104])
def test_stale_or_out_of_crossing_fall_frame_rejected(ts):
    r = Replay()
    assert r.crossing()
    assert not r.owner.burst_frame(scene(B, seq=12, ts=ts))
    assert r.land()
    assert not any(o.stage == Stage.FALL for o in r.j.observations)


def test_previous_journey_fall_alias_rejected_even_inside_new_time_window():
    r = Replay()
    old = r.owner.c4_only(
        KnownObject(),
        episode_id="prior",
        generation=1,
        scene=scene(B, seq=8, ts=99.8),
        alias=B,
    )
    assert old
    assert r.crossing()
    assert not r.owner.burst_frame(scene(B, seq=12, ts=100.4))
    assert r.j.crossing_rejected and not r.land()


def test_resident_outside_arrival_region_prevents_exclusive_crossing_claim():
    r = Replay()
    resident = Detection(replace(B, track_id=9), (125, 20, 145, 50))
    baseline = scene(B, seq=10, ts=100.1, detections=[resident])
    assert not r.owner.begin_crossing(
        r.j,
        release=scene(seq=2, ts=100.2),
        empty_c4=baseline,
        c3_region=REGION,
        c4_region=REGION,
    )
    assert not r.land() and not r.j.crossing_confirmed


def test_unconnected_singleton_fall_labels_do_not_inherit_landing_identity():
    r = Replay()
    assert r.crossing()
    for track, seq in ((103, 12), (104, 13)):
        assert r.owner.burst_frame(
            scene(replace(B, track_id=track), seq=seq, ts=100.3 + (seq - 12) * 0.1)
        )
    assert r.land()
    assert {a.track_id for a in r.j.aliases} == {42, 105}
    assert not any(o.stage == Stage.FALL for o in r.j.observations)


def test_c3_cached_numeric_id_does_not_override_older_frame_generation(monkeypatch):
    from test_ready_channel_crops import Harness, piece

    h = Harness(monkeypatch)
    h.populated()
    h.tick(1000.3, [piece(deg=12)])
    h.service.channel_crop_collector = h.collector
    r = Replay()
    alias = replace(A, track_id=7, generation=8)
    s = Scene(
        3, A.epoch, 20, 1000.3, h.sample[1].bgr, (Detection(alias, (20, 20, 45, 45)),)
    )
    j = r.owner.start_c3(
        KnownObject(), episode_id="new", generation=13, scene=s, alias=alias
    )
    evidence = {"ts": 1000.3, "material": [{"id": 7, "bbox": (20, 20, 45, 45)}]}

    def previous_generation(image):
        return replace(
            s,
            timestamp=image["frame_ts"],
            detections=(Detection(replace(alias, generation=7), (20, 20, 45, 45)),),
        )

    assert (
        retain_ready_history(r.owner, j, h.service, evidence, s, previous_generation)
        == 0
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(timestamp=None),
        dict(sequence=-1),
        dict(bgr=None),
        dict(detections=(None,)),
        dict(complete=False),
    ],
)
def test_malformed_optional_evidence_is_omitted_at_every_entry_point(change):
    r = Replay()
    bad = replace(scene(), **change)
    assert (
        r.owner.start_c3(
            KnownObject(), episode_id="bad", generation=1, scene=bad, alias=A
        )
        is None
    )
    assert not r.owner.begin_crossing(
        r.j,
        release=bad,
        empty_c4=scene(B, detections=[]),
        c3_region=REGION,
        c4_region=REGION,
    )
    assert r.crossing()
    assert not r.owner.burst_frame(bad)
    assert not r.owner.land(r.j, scene=bad, c3_after=bad)
    assert not r.owner.observe(r.j, bad, A, Stage.C3_HISTORY)


def test_follower_present_at_exit_blocks_upstream_association_not_transport():
    r = Replay()
    assert r.crossing()
    follower = replace(A, track_id=43)
    assert not r.owner.land(
        r.j,
        scene=scene(B, seq=14, ts=100.6),
        c3_after=scene(follower, seq=3, ts=100.65),
    )
    assert not r.j.crossing_confirmed


def test_two_simultaneous_crossings_cannot_feed_a_third_journey():
    r = Replay()
    assert r.crossing()
    for number in (43, 44):
        alias = replace(A, track_id=number)
        j = r.owner.start_c3(
            KnownObject(),
            episode_id=f"t-{number}",
            generation=number,
            scene=scene(alias),
            alias=alias,
        )
        assert not r.owner.begin_crossing(
            j,
            release=scene(alias, seq=2, ts=100.2),
            empty_c4=r.empty,
            c3_region=REGION,
            c4_region=REGION,
        )
        assert j.crossing_rejected
    assert r.j.crossing_rejected and not r.land()


def test_generation_rollover_invalidates_old_alias_and_late_result():
    r = full_replay()
    tasks = []
    assert r.owner.submit(r.j, lambda req: {"items": []}, launch=tasks.append)
    newer = replace(B, generation=3)
    next_j = r.owner.c4_only(
        KnownObject(),
        episode_id="next",
        generation=13,
        scene=scene(newer, seq=20, ts=102),
        alias=newer,
    )
    assert next_j
    assert not r.owner.observe(r.j, scene(B, seq=21, ts=102.1), B, Stage.SETTLED)
    r.owner.close(r.j)
    tasks[0]()
    assert r.j.result is None and next_j.result is None


def test_track_loss_keeps_copied_images_and_uuid():
    r = full_replay()
    before = list(r.j.observations)
    assert not r.owner.observe(
        r.j, scene(B, seq=16, ts=101, detections=[]), B, Stage.SETTLED
    )
    assert r.j.observations == before and r.j.uuid == UUID
    r.owner.set_epoch(4, "new-tracker")
    assert not r.owner.observe(r.j, scene(B, seq=17, ts=101.1), B, Stage.SETTLED)
    assert r.owner.prepare_request(r.j)


def test_near_duplicate_and_in_plane_rotation_are_not_four_views():
    image = pose(4)
    observations = [
        Observation(
            Stage.SETTLED, B, (4, B.epoch, i), 101 + i, np.rot90(image, i).copy()
        )
        for i in range(4)
    ]
    assert len(select_views(observations, include_upstream=False)) == 1


def test_single_c4_only_request_once_even_after_provider_error():
    r = Replay()
    piece = KnownObject()
    j = r.owner.c4_only(
        piece,
        episode_id="c4-only",
        generation=1,
        scene=scene(B, seq=10, ts=100.2),
        alias=B,
    )
    calls = []

    def provider(req):
        calls.append(req)
        raise RuntimeError("offline provider error")

    assert r.owner.submit(j, provider, launch=lambda work: work())
    assert not r.owner.submit(j, provider, launch=lambda work: work())
    assert len(calls) == 1 and len(calls[0].images) == 1
    assert j.result.error == "offline provider error"
    assert piece.classification_status == KnownObject().classification_status


def test_concurrent_submission_is_exactly_one_call():
    r = full_replay()
    calls = []
    threads = [
        threading.Thread(
            target=lambda: r.owner.submit(
                r.j, lambda req: calls.append(req), launch=lambda work: work()
            )
        )
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


def test_original_brickognize_multipart_contains_selected_images_once(monkeypatch):
    r = full_replay()
    sent = []

    def send(session, request, **kwargs):
        msg = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + request.headers["Content-Type"].encode()
            + b"\r\n\r\n"
            + request.body
        )
        sent.append([part.get_payload(decode=True) for part in msg.iter_parts()])
        response = requests.Response()
        response.status_code = 200
        response._content = (
            b'{"items":[{"id":"3001","category":"Brick","score":0.99}],"colors":[]}'
        )
        return response

    monkeypatch.setattr(requests.sessions.Session, "send", send)
    assert r.owner.submit(r.j, brickognize_provider(), launch=lambda work: work())
    assert len(sent) == 1 and len(sent[0]) == 4
    for jpeg, observation in zip(sent[0], r.j.request.images):
        actual = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        assert actual.shape == observation.bgr.shape
        assert np.abs(actual.astype(float) - observation.bgr).mean() < 5
    assert r.j.result.response["items"][0]["id"] == "3001"


def test_legacy_detected_burst_without_exact_proof_never_contributes():
    r = full_replay()
    detail = {
        "global_id": 105,
        "created_at": 100.5,
        "drop_zone_burst": [
            {
                "timestamp": 100.4,
                "detected": True,
                "phase": "pre",
                "crop_jpeg_b64": "unproven",
            }
        ],
    }
    ref = HistoryReference(B, 105, 100.5)
    assert retain_track_history(r.owner, r.j, detail, ref, lambda record: None) == 0
    assert (
        retain_track_history(
            r.owner, r.j, detail, replace(ref, created_at=99), lambda record: None
        )
        == 0
    )


def test_existing_c4_history_snapshots_require_paired_alias_and_incarnation():
    r = full_replay()
    snap = {"captured_ts": 101, "piece_jpeg_b64": "existing-format"}
    detail = {
        "global_id": 105,
        "created_at": 100.5,
        "segments": [{"source_role": "carousel", "sector_snapshots": [snap]}],
    }
    ref = HistoryReference(B, 105, 100.5)
    assert (
        retain_track_history(
            r.owner, r.j, detail, ref, lambda record: scene(B, seq=20, ts=101, seed=6)
        )
        == 1
    )
    assert r.j.observations[-1].stage == Stage.SETTLED


def test_recorded_same_piece_quality_fixtures_prefer_starred_crop():
    fixtures = Path(__file__).parent / "fixtures/crop_quality"
    blurred = cv2.imread(str(fixtures / "blur_e683f68f_seq0.jpg"))
    sharp = cv2.imread(str(fixtures / "star_e683f68f_seq3.jpg"))
    assert blurred is not None and sharp is not None
    observations = [
        Observation(Stage.SETTLED, B, (4, B.epoch, i), 101 + i, image)
        for i, image in enumerate((blurred, sharp))
    ]
    selected = select_views(observations, include_upstream=False)
    assert selected[0] is observations[1]


def test_image_cap_and_malformed_optional_frames_do_not_change_piece_transport_fields():
    r = full_replay()
    before = vars(r.piece).copy()
    for n in range(20):
        r.owner.observe(
            r.j, scene(B, seq=16 + n, ts=101 + n / 10, seed=n + 10), B, Stage.SETTLED
        )
    assert sum(o.stage == Stage.SETTLED for o in r.j.observations) <= 8
    assert not r.owner.observe(r.j, replace(scene(B), bgr=None), B, Stage.SETTLED)
    assert vars(r.piece) == before
    assert len(r.owner.prepare_request(r.j).images) <= 4


@pytest.mark.parametrize("phase", ["burst", "landing"])
def test_second_c4_object_anywhere_contradicts_exclusive_arrival(phase):
    r = Replay()
    assert r.crossing()
    second = Detection(replace(B, track_id=999), (125, 20, 145, 50))
    s = scene(B, seq=14, ts=100.6, detections=[Detection(B, BOX), second])
    if phase == "burst":
        assert not r.owner.burst_frame(s)
    else:
        assert not r.owner.land(
            r.j, scene=s, c3_after=scene(seq=3, ts=100.65, detections=[])
        )
    assert r.j.crossing_rejected and not r.j.crossing_confirmed


def test_visible_c3_follower_prevents_exclusive_crossing_claim():
    r = Replay()
    follower = Detection(replace(A, track_id=43), (125, 20, 145, 50))
    assert not r.owner.begin_crossing(
        r.j,
        release=scene(seq=2, ts=100.2, detections=[Detection(A, BOX), follower]),
        empty_c4=scene(B, seq=10, ts=100.1, detections=[]),
        c3_region=REGION,
        c4_region=REGION,
    )


def test_submit_queues_selection_and_provider_without_waiting_for_images(monkeypatch):
    r = full_replay()
    queued, called = [], []
    import recognition_journey as module

    original = module.select_views

    def select(*args, **kwargs):
        called.append("selected")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "select_views", select)
    assert r.owner.submit(
        r.j, lambda req: called.append("provider"), launch=queued.append
    )
    assert not called and len(queued) == 1
    assert not r.owner.observe(r.j, scene(B, seq=30, ts=102), B, Stage.SETTLED)
    queued[0]()
    assert called == ["selected", "provider"]


def test_close_releases_manager_images_and_preserves_once_only_uuid():
    r = full_replay()
    r.owner.close(r.j)
    assert UUID not in r.owner.journeys
    assert (
        r.owner.start_c3(
            r.piece,
            episode_id="new",
            generation=13,
            scene=scene(seq=20, ts=102),
            alias=A,
        )
        is None
    )
    assert (
        r.owner.c4_only(
            r.piece,
            episode_id="new",
            generation=13,
            scene=scene(B, seq=20, ts=102),
            alias=B,
        )
        is None
    )


def test_blank_optional_view_dropped_while_single_valid_c4_still_selected():
    r = full_replay()
    observations = [o for o in r.j.observations if o.stage == Stage.SETTLED]
    observations.append(
        Observation(
            Stage.C3_HISTORY,
            A,
            (3, A.epoch, 99),
            100,
            np.full((80, 80, 3), 170, np.uint8),
        )
    )
    selected = select_views(observations, include_upstream=True)
    assert len(selected) == 1 and selected[0].stage == Stage.SETTLED


@pytest.mark.parametrize(
    "alias", [A, replace(A, generation=8), replace(A, track_id=43)]
)
def test_c3_material_outside_exit_region_cannot_prove_single_piece_departure(alias):
    r = Replay()
    assert r.crossing()
    after = scene(seq=3, ts=100.65, detections=[Detection(alias, (125, 20, 145, 50))])
    assert not r.owner.land(r.j, scene=scene(B, seq=14, ts=100.6), c3_after=after)
    assert not r.j.crossing_confirmed


def test_slow_selection_does_not_hold_the_control_tick_lock(monkeypatch):
    import recognition_journey as module

    r = full_replay()
    entered, release = threading.Event(), threading.Event()
    original = module.select_views

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "select_views", delayed)
    assert r.owner.submit(r.j, lambda request: {})
    assert entered.wait(2)
    try:
        assert r.j.lock.acquire(blocking=False)
        r.j.lock.release()
        assert not r.owner.submit(r.j, lambda request: {})
        assert not r.owner.observe(r.j, scene(B, seq=30, ts=102), B, Stage.SETTLED)
    finally:
        release.set()
