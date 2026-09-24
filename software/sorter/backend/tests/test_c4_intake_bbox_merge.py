"""C4 arrival evidence must survive merging boxes across a pocket divider."""

from dataclasses import replace

import numpy as np
import pytest
from perception import arcs

from perception.arcs import (
    mergeC4BboxesPreservingIntake,
    mergeNearbyBboxes,
    orderedPieceObservations,
)
from perception.channel import ChannelDef


@pytest.fixture(autouse=True)
def clear_zone_cache():
    arcs._region_lookup_cache.clear()
    yield
    arcs._region_lookup_cache.clear()


def channel(drop=range(0, 35)):
    return ChannelDef(
        channel_id=4,
        camera_source_id="classification_channel",
        center=(100, 100),
        radius1_angle_image=0,
        mask=np.ones((400, 400), dtype=np.uint8),
        drop_sections=frozenset(drop),
        exit_sections=frozenset(range(180, 200)),
    )


def intake_boxes(boxes, ch):
    return {b for _, _, zone, b in orderedPieceObservations(boxes, ch) if zone == 1}


def test_arrived_box_is_not_swallowed_by_adjacent_owned_pocket():
    # Actual failure shape: red in DROP, tan in the preceding pocket, overlapping
    # rectangles across a diagonal divider. Their union's COM is outside DROP.
    ch = channel()
    red = (200, 145, 270, 205)
    tan = (140, 195, 220, 275)
    assert intake_boxes([red, tan], ch) == {red}
    old_union = [b for b, _ in mergeNearbyBboxes([red, tan], 14)]
    assert len(old_union) == 1
    assert intake_boxes(old_union, ch) == set()
    fixed = [b for b, _ in mergeC4BboxesPreservingIntake([red, tan], 14, ch)]
    assert set(fixed) == {red, tan}
    assert intake_boxes(fixed, ch) == {red}


def test_same_intake_oversegmentation_still_merges():
    ch = channel()
    boxes = [(200, 130, 230, 160), (225, 135, 250, 165)]
    clusters = mergeC4BboxesPreservingIntake(boxes, 14, ch)
    assert clusters == [((200, 130, 250, 165), boxes)]
    assert intake_boxes([clusters[0][0]], ch)


def test_nonintake_neighbors_cannot_merge_into_false_arrival():
    # Both COMs outside a narrow calibrated DROP arc, their union inside it.
    ch = channel(range(30, 40))
    boxes = [(200, 130, 260, 190), (180, 190, 240, 250)]
    assert not intake_boxes(boxes, ch)
    old_union = [b for b, _ in mergeNearbyBboxes(boxes, 14)]
    assert intake_boxes(old_union, ch)
    fixed = [b for b, _ in mergeC4BboxesPreservingIntake(boxes, 14, ch)]
    assert set(fixed) == set(boxes)
    assert not intake_boxes(fixed, ch)


def test_missing_zone_attribution_does_not_merge():
    ch = replace(channel(), exit_sections=frozenset())
    boxes = [(200, 130, 230, 160), (225, 135, 250, 165)]
    assert mergeC4BboxesPreservingIntake(boxes, 14, ch) == [(b, [b]) for b in boxes]


def test_empty_input():
    assert mergeC4BboxesPreservingIntake([], 14, channel()) == []
