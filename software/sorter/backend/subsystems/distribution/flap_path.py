"""The complete flap configuration required by a distribution transaction."""


def validate_flaps(servos, target_layer: int | None) -> None:
    if not servos:
        raise RuntimeError("No distribution flaps are configured")
    if target_layer is not None and not 0 <= target_layer < len(servos):
        raise RuntimeError("Destination flap is not configured")
    # Keep the existing contract: destination closed, all other layers open.
    # Layer eligibility must never remove a physical door from this path.
    for index, servo in enumerate(servos):
        if not servo.available or not getattr(servo, "is_calibrated", True):
            raise RuntimeError(f"Layer {index + 1} flap is unavailable or uncalibrated")


def flap_settled(servo, *, opened: bool) -> bool:
    if not servo.available or not getattr(servo, "is_calibrated", True):
        raise RuntimeError("flap is unavailable or uncalibrated")
    if not servo.stopped:
        return False
    reached = servo.isOpen() if opened else servo.isClosed()
    if reached is not True:
        raise RuntimeError("flap is stopped but its required position is unknown or incorrect")
    return True


def flap_path_settled(servos, target_layer: int | None) -> bool:
    validate_flaps(servos, target_layer)
    settled = True
    for index, servo in enumerate(servos):
        try:
            if not flap_settled(servo, opened=index != target_layer):
                settled = False
        except Exception as exc:
            raise RuntimeError(f"Layer {index + 1}: {exc}") from exc
    return settled
