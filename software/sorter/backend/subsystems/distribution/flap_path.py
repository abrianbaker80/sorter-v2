"""Full route qualification from fresh driver evidence; never receiving proof."""

from __future__ import annotations

from smart_bins_native_custody import CustodyRefused


def qualify(
    servos, *, layer_count: int, target_layer: int | None, commands: dict
) -> dict:
    if layer_count <= 0 or len(servos) < layer_count:
        raise CustodyRefused("Required route flap is missing")
    if target_layer is not None and not 0 <= target_layer < layer_count:
        raise CustodyRefused("Invalid intended route layer")
    observations = []
    for index in range(layer_count):
        servo = servos[index]
        if not getattr(servo, "available", False) or not getattr(
            servo, "is_calibrated", False
        ):
            raise CustodyRefused(f"Layer {index} flap unavailable or uncalibrated")
        if commands.get(index) is not True:
            raise CustodyRefused(f"Layer {index} flap command not acknowledged")
        evidence = servo.feedback()
        expected = "is_closed" if index == target_layer else "is_open"
        if (
            evidence.get("command_outcome") != "ACCEPTED"
            or evidence.get("motion_state_valid") is not True
            or evidence.get("stopped") is not True
            or evidence.get("position_valid") is not True
            or evidence.get(expected) is not True
            or evidence.get("pending_target") is not None
        ):
            raise CustodyRefused(f"Layer {index} flap route is unconfirmed")
        observations.append(
            {
                "layer": index,
                "expected": expected,
                "position": evidence["position"],
                "motion_observed_at": evidence["motion_observed_at"],
                "position_observed_at": evidence["position_observed_at"],
                "command_outcome": "ACCEPTED",
            }
        )
    return {
        "target_layer": target_layer,
        "flaps": observations,
        "receiving_evidence": False,
    }
