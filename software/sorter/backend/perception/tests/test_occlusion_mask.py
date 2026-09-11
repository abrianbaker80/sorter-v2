import numpy as np

from perception.inference import _apply_occlusion_mask, _build_occlusion_ring


def test_occlusion_is_replaced_by_nearby_channel_color_without_mutating_frame():
    image = np.full((80, 80, 3), 40, dtype=np.uint8)
    image[30:50, 30:50] = 240
    original = image.copy()
    occlusion = np.zeros((80, 80), dtype=np.uint8)
    occlusion[30:50, 30:50] = 255
    channel = np.full((80, 80), 255, dtype=np.uint8)

    ring = _build_occlusion_ring(occlusion, channel)
    result = _apply_occlusion_mask(image, occlusion, ring)

    assert ring is not None
    assert np.array_equal(image, original)
    assert np.all(result[30:50, 30:50] == 40)
    assert np.all(result[:30] == image[:30])


def test_no_occlusion_returns_same_image_reference():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    assert _apply_occlusion_mask(image, None, None) is image
