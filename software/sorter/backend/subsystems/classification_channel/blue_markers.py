# Extracted from the installed passive blue-marker estimator.
import math


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
    mask = (
        (hsv[:, :, 0] > 85)
        & (hsv[:, :, 0] < 115)
        & (hsv[:, :, 1] > 95)
        & (rr > 45)
        & (rr < 115)
    ).astype("uint8")
    count, labels, stats, centers = cv2.connectedComponentsWithStats(mask)
    components = []
    for j in range(1, count):
        if stats[j, 4] < 12:
            continue
        x, y = centers[j]
        radii = rr[labels == j]
        components.append(
            (
                math.degrees(math.atan2(y - cy, x - cx)) % 360,
                int(stats[j, 4]),
                float(radii.min()),
                float(radii.max()),
            )
        )
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
        theta = (
            math.degrees(
                math.atan2(
                    sum(c[1] * math.sin(math.radians(c[0])) for c in group),
                    sum(c[1] * math.cos(math.radians(c[0])) for c in group),
                )
            )
            % 360
        )
        angles.append(theta)
        radial = sorted(group, key=lambda c: c[2])
        if len(radial) == 2 and radial[1][2] - radial[0][3] > 8:
            dashed.append(theta)
        elif len(radial) != 1 or radial[0][3] - radial[0][2] < 20:
            return None
        quality.append(
            {
                "angle": theta,
                "components": len(group),
                "area_px": sum(c[1] for c in group),
                "radial_span_px": max(c[3] for c in group) - min(c[2] for c in group),
                "component_angle_spread_deg": max(
                    abs((c[0] - theta + 180) % 360 - 180) for c in group
                ),
            }
        )
    angles.sort()
    if len(dashed) > 1:
        return None
    if dashed:
        # Anchor identities to the unique dashed spoke. Off-grid extras are
        # ignored; duplicate plausible identities remain ambiguous.
        ids = {}
        extra = []
        for angle in angles:
            index = round(((angle - dashed[0]) % 360) / 36) % 10
            error = abs((angle - dashed[0] - 36 * index + 180) % 360 - 180)
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
    gaps = [
        (angles[(i + 1) % len(angles)] - angles[i]) % 360 for i in range(len(angles))
    ]
    return {
        "angles": angles,
        "dashed": dashed[0] if dashed else None,
        "spacing": gaps,
        "quality": quality,
        "extra_angles": extra,
        "missing_indices": sorted(set(range(10)) - set(ids)) if dashed else [],
        "phase_mod36_degrees": circular_median([x % 36 for x in angles], 36),
    }


def circular_median(values, period=360):
    import statistics

    anchor = values[0]
    return (
        anchor
        + statistics.median(
            [(v - anchor + period / 2) % period - period / 2 for v in values]
        )
    ) % period


def identities(observation):
    if observation is None or observation.get("dashed") is None:
        return None
    result = {}
    for angle in observation["angles"]:
        index = round(((angle - observation["dashed"]) % 360) / 36) % 10
        if abs((angle - observation["dashed"] - index * 36 + 180) % 360 - 180) > 3:
            continue
        if index in result:
            return None
        result[index] = angle
    return result if 0 in result else None


def phase(image, center):
    """Absolute phase assigned by dashed identity and fitted to >=7 solids.

    The unique dashed marker resolves 36-degree aliases independently of
    commands. The inherited 3-degree constellation bound is a quality check,
    not a claim about absolute angular accuracy.
    """
    spokes = identities(extract(image, center))
    if spokes is None or len(spokes) < 8:
        return None
    votes = [(angle - index * 36) % 360 for index, angle in spokes.items() if index]
    fitted = circular_median(votes)
    if (
        max(abs((v - fitted + 180) % 360 - 180) for v in votes) > 3
        or abs((spokes[0] - fitted + 180) % 360 - 180) > 3
    ):
        return None
    return fitted
