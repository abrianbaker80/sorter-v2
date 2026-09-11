"""Secondary (foreign/occlusion) zone tests.

Secondary zones are polygons a camera observes outside its primary channel.
Foreign channel zones remain display/tag-only. Calibrated occlusions neutralize
fixed hardware before inference and exclude any residual detector box centered
on that hardware from primary attribution.
"""

import math
from types import SimpleNamespace
from typing import Any, cast

import numpy as np

from perception.arcs import attributeBboxes, bboxInsideMask
from perception.channel import SecondaryZone, buildChannelDef
from perception.detection import Detection
from perception.inference import (
    _apply_inference_scope_mask,
    _build_inference_scope_mask,
)
from perception.service import PerceptionService


IMAGE_W = 400
IMAGE_H = 400
CENTER = (200, 200)
RADIUS = 140
BBOX_HALF = 6


def _annulus_polygon(inner=RADIUS - 30, outer=RADIUS + 30) -> np.ndarray:
    outer_pts, inner_pts = [], []
    for i in range(64):
        theta = 2.0 * math.pi * i / 64.0
        outer_pts.append([CENTER[0] + outer * math.cos(theta), CENTER[1] + outer * math.sin(theta)])
        inner_pts.append([CENTER[0] + inner * math.cos(theta), CENTER[1] + inner * math.sin(theta)])
    return np.array(outer_pts + list(reversed(inner_pts)), dtype=np.int32)


def _bbox_at_angle(angle_deg: float, radius: float = RADIUS):
    rad = math.radians(angle_deg)
    cx = CENTER[0] + radius * math.cos(rad)
    cy = CENTER[1] + radius * math.sin(rad)
    return (int(cx - BBOX_HALF), int(cy - BBOX_HALF), int(cx + BBOX_HALF), int(cy + BBOX_HALF))


def _square(cx, cy, half):
    return [[cx - half, cy - half], [cx + half, cy - half], [cx + half, cy + half], [cx - half, cy + half]]


def _channel(secondary=None):
    return buildChannelDef(
        channel_id=4,
        polygon=_annulus_polygon(),
        frame_shape=(IMAGE_H, IMAGE_W),
        section_zero_angle=0.0,
        drop_arc=(75.0, 105.0),
        exit_arc=(255.0, 285.0),
        precise_arc=(265.0, 285.0),
        arc_center=CENTER,
        secondary_zone_entries=secondary,
    )


def test_secondary_zone_builds_mask():
    ch = _channel([{"id": "sz1", "source_channel": 3, "zone_type": "exit", "points": _square(50, 50, 20)}])
    assert len(ch.secondary_zones) == 1
    z = ch.secondary_zones[0]
    assert z.id == "sz1" and z.source_channel == 3 and z.zone_type == "exit"
    assert z.mask.shape == (IMAGE_H, IMAGE_W)
    assert bool(z.mask[50, 50])          # center filled
    assert not bool(z.mask[200, 200])    # far away, empty


def test_occlusion_zone_builds_hot_path_union_mask():
    ch = _channel(
        [
            {
                "id": "divider",
                "source_channel": 3,
                "zone_type": "occlusion",
                "points": _square(50, 50, 20),
            }
        ]
    )
    assert ch.secondary_zones[0].zone_type == "occlusion"
    assert ch.occlusion_mask is not None
    assert bool(ch.occlusion_mask[50, 50])
    assert not bool(ch.occlusion_mask[200, 200])


def test_inference_scope_unions_foreign_zone_but_not_occlusion_only_area():
    ch = _channel(
        [
            {
                "id": "c3-throat",
                "source_channel": 3,
                "zone_type": "drop",
                "points": _square(50, 50, 20),
            },
            {
                "id": "outside-hardware",
                "source_channel": 3,
                "zone_type": "occlusion",
                "points": _square(350, 350, 20),
            },
        ]
    )

    scope = _build_inference_scope_mask(ch)

    assert bool(scope[50, 50])
    assert not bool(ch.mask[50, 50])
    assert not bool(scope[350, 350])


def test_inference_scope_fill_preserves_source_and_visible_pixels():
    image = np.full((4, 5, 3), 17, dtype=np.uint8)
    original = image.copy()
    scope = np.zeros((4, 5), dtype=np.uint8)
    scope[1:3, 2:4] = 255

    masked = _apply_inference_scope_mask(image, scope)

    assert np.array_equal(image, original)
    assert np.all(masked[1:3, 2:4] == 17)
    assert np.all(masked[scope == 0] == 230)


def test_occlusion_is_not_reported_as_foreign_channel_occupancy():
    ch = _channel(
        [
            {
                "id": "divider",
                "source_channel": 3,
                "zone_type": "occlusion",
                "points": _square(50, 50, 20),
            }
        ]
    )
    worker = SimpleNamespace(
        latest_detections=[
            Detection(
                bbox=(44, 44, 56, 56),
                in_primary=False,
                secondary_zone_ids=("divider",),
            )
        ]
    )
    service = PerceptionService(
        channels={4: ch},
        captures={},
        runtimes={},
        slots={},
        workers={4: cast(Any, worker)},
    )

    assert not service.secondary_zone_occupied(4, source_channel=3)
    assert service.secondary_zone_occupied(4, zone_type="occlusion")


def test_membership_tagging():
    ch = _channel([{"id": "sz1", "source_channel": 3, "zone_type": "exit", "points": _square(50, 50, 20)}])
    z = ch.secondary_zones[0]
    assert bboxInsideMask((44, 44, 56, 56), z.mask)  # center (50,50) inside
    assert not bboxInsideMask((194, 194, 206, 206), z.mask)  # center (200,200) outside


def test_rescale_matches_primary():
    # Saved at 200x200, frame is 400x400 -> 2x. A zone square centered at (25,25)
    # in saved space must land centered at (50,50) in frame space.
    ch = buildChannelDef(
        channel_id=4,
        polygon=(_annulus_polygon().astype(np.float64) / 2.0).astype(np.int32),
        frame_shape=(IMAGE_H, IMAGE_W),
        section_zero_angle=0.0,
        drop_arc=(75.0, 105.0),
        exit_arc=(255.0, 285.0),
        precise_arc=None,
        arc_center=(CENTER[0] / 2.0, CENTER[1] / 2.0),
        saved_resolution=(IMAGE_W / 2.0, IMAGE_H / 2.0),
        secondary_zone_entries=[{"id": "sz1", "source_channel": 3, "zone_type": "drop", "points": _square(25, 25, 10)}],
    )
    z = ch.secondary_zones[0]
    assert bool(z.mask[50, 50])
    assert not bool(z.mask[25, 25])


def test_primary_attribution_unchanged_by_secondary_zones():
    plain = _channel(None)
    # Secondary zone deliberately overlaps the exit arc region to prove it does
    # not leak into the primary attribution.
    exit_bbox = _bbox_at_angle(270.0)
    sz_points = _square((exit_bbox[0] + exit_bbox[2]) // 2, (exit_bbox[1] + exit_bbox[3]) // 2, 40)
    withzones = _channel([{"id": "sz1", "source_channel": 3, "zone_type": "exit", "points": sz_points}])

    bboxes = [_bbox_at_angle(90.0), _bbox_at_angle(270.0)]
    assert attributeBboxes(bboxes, plain)[:5] == attributeBboxes(bboxes, withzones)[:5]
    # And the primary mask / section sets are byte-identical.
    assert np.array_equal(plain.mask, withzones.mask)
    assert plain.drop_sections == withzones.drop_sections
    assert plain.exit_sections == withzones.exit_sections
    assert plain.precise_sections == withzones.precise_sections


def test_malformed_entries_skipped():
    ch = _channel([
        {"id": "bad", "source_channel": 3, "zone_type": "exit", "points": [[1, 1], [2, 2]]},  # <3 pts
        "not-a-dict",
        {"id": "ok", "source_channel": 2, "zone_type": "drop", "points": _square(60, 60, 15)},
    ])
    assert [z.id for z in ch.secondary_zones] == ["ok"]
