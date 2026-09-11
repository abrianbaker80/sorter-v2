import time
import queue
import random
from typing import Optional
import server.shared_state as shared_state
from states.base_state import BaseState
from subsystems.shared_variables import SharedVariables
from .states import DistributionState
from .chute import Chute, BinAddress
from irl.bin_layout import DistributionLayout, Bin, extractCategories
from irl.config import IRLInterface
from global_config import GlobalConfig
from sorting_profile import SortingProfile, MISC_CATEGORY
from blob_manager import setBinCategories
from defs.events import PauseCommandData, PauseCommandEvent
from defs.known_object import PieceStage
from utils.event import knownObjectToEvent


BINS_FULL_ALERT_PREFIX = "No bin available"
MISC_PASSTHROUGH_ALERT_PREFIX = "Misc passthrough"
CHUTE_JAM_ALERT_PREFIX = "Chute jam"
SERVO_BUS_ALERT_PREFIX = "Servo bus offline"
HARVEST_RUNTIME_ALERT_PREFIX = "Harvest live sorting paused"
DISTRIBUTION_CHUTE_JAM_INCIDENT_KIND = "distribution_chute_jam"
DISTRIBUTION_SERVO_BUS_OFFLINE_INCIDENT_KIND = "distribution_servo_bus_offline"
DISTRIBUTION_NO_BIN_AVAILABLE_INCIDENT_KIND = "distribution_no_bin_available"
# Beyond how many ms after the estimated move time do we conclude the
# chute / servo is stuck? 3× the expected move is generous but catches
# a real jam reliably.
CHUTE_MOVE_TIMEOUT_MS = 6000
CHUTE_MOVE_TIMEOUT_MULTIPLIER = 3.0


def _incidentHandlingOff(kind: str) -> bool:
    try:
        from toml_config import incidentHandlingOff

        return bool(incidentHandlingOff(kind))
    except Exception:
        return False


def _allowMultiCategoryBins() -> bool:
    try:
        from toml_config import getBinAssignmentConfig

        return bool(
            getBinAssignmentConfig().get("allow_multiple_categories_per_bin", False)
        )
    except Exception:
        return False


class Positioning(BaseState):
    def __init__(
        self,
        irl: IRLInterface,
        gc: GlobalConfig,
        shared: SharedVariables,
        chute: Chute,
        layout: DistributionLayout,
        sorting_profile: SortingProfile,
        event_queue: queue.Queue,
    ):
        super().__init__(irl, gc)
        self.shared = shared
        self.chute = chute
        self.layout = layout
        self.sorting_profile = sorting_profile
        self.event_queue = event_queue
        self._phase: str = "init"
        self._target_address: BinAddress | None = None
        self._door_servo_index: int | None = None
        self._door_servo_indices: list[int] = []
        self._state_entered_at: float = 0.0
        self._moving_started_at: float = 0.0
        self._piece = None
        self._occupancy_state: str | None = None
        self._blocked_layers: set[int] = set()
        self._servo_offline_layers: set[int] = set()
        self._jam_pause_enqueued: bool = False
        self._servo_bus_pause_enqueued: bool = False
        self._harvest_pause_enqueued: bool = False
        self._chute_move_estimated_ms: int = 0

    def _setOccupancyState(self, state_name: str) -> None:
        if self._occupancy_state == state_name:
            return
        prev_state = self._occupancy_state
        self._occupancy_state = state_name
        self.gc.runtime_stats.observeStateTransition(
            "distribution.occupancy",
            prev_state,
            state_name,
        )

    def step(self) -> Optional[DistributionState]:
        now = time.monotonic()

        if self._phase == "init":
            # Fresh evaluation per piece — an earlier transient servo
            # glitch must not permanently disable a layer.
            self._blocked_layers.clear()
            self._servo_offline_layers.clear()
            self._setOccupancyState("positioning.select_target_bin")
            self._state_entered_at = now
            transport = self.shared.transport
            piece = (
                transport.getPieceForDistributionPositioning()
                if transport is not None
                else None
            )
            if piece is None:
                self.logger.warn("Positioning: no piece ready for distribution")
                self._setOccupancyState("positioning.wait_piece_for_distribution")
                return DistributionState.IDLE

            if getattr(self.shared, "sample_collection_mode", False):
                self.logger.info(
                    "Positioning: sample collection mode — opening all layer doors for discard passthrough"
                )
                self._clearBinsFullAlertIfOwned()
                self._clearChuteJamAlertIfOwned()
                self._openAllDoorsForPassthrough()
                piece.stage = PieceStage.distributing
                piece.distributing_at = time.time()
                piece.distribution_target_selected_at = piece.distributing_at
                piece.destination_bin = None
                piece.updated_at = time.time()
                self._piece = piece
                self.event_queue.put(knownObjectToEvent(piece))
                self._setOccupancyState("positioning.sample_collection_passthrough")
                return DistributionState.READY

            harvest_route = self._reserveHarvestRoute(piece)
            if harvest_route is False:
                return DistributionState.IDLE

            if piece.too_big:
                # Oversize for any real bin — send it down the center of the
                # chute to the misc bottom bin (open every usable door so it
                # falls straight through). Never claims a bin, never raises a
                # no-bin incident.
                self.logger.info(
                    f"Positioning: piece {piece.uuid} is too big "
                    f"({piece.max_dimension_mm}mm) — passthrough to misc bottom bin"
                )
                self._clearBinsFullAlertIfOwned()
                self._clearChuteJamAlertIfOwned()
                self._openAllDoorsForPassthrough()
                piece.stage = PieceStage.distributing
                piece.distributing_at = time.time()
                piece.distribution_target_selected_at = piece.distributing_at
                piece.category_id = MISC_CATEGORY
                piece.destination_bin = None
                piece.updated_at = time.time()
                self._piece = piece
                self.event_queue.put(knownObjectToEvent(piece))
                self._setOccupancyState("positioning.passthrough_too_big")
                return DistributionState.READY

            harvest_active = isinstance(harvest_route, tuple)
            if harvest_active:
                address, category_id = harvest_route
            elif piece.part_id is not None:
                category_id = self.sorting_profile.getCategoryIdForPart(piece.part_id, piece.color_id)
            else:
                category_id = MISC_CATEGORY
            # High-value override: a piece whose Hive moving-average price
            # clears the profile's high_value_routing threshold is rerouted into
            # the configured category (e.g. Yellow/Orange Tiles), so it lands in
            # that category's bin regardless of its normal classification.
            high_value_category = (
                None
                if harvest_active
                else self.sorting_profile.highValueCategoryId(piece.moving_avg_price)
            )
            if high_value_category is not None:
                self.logger.info(
                    f"Positioning: piece {piece.uuid} ({piece.part_id}) moving-avg "
                    f"${piece.moving_avg_price} clears high-value threshold — routing to "
                    f"category {high_value_category} (was {category_id})"
                )
                category_id = high_value_category
                piece.high_value_routed = True
            # Inventory routing: a piece absent from the active .bsx (resolved at
            # classification time via a live BrickLink-id lookup) routes within the
            # parallel not-in-inventory bin pool, sub-sorted by its normal category.
            # Everyone else routes among the normal bins; the pools never mix.
            route_not_in_inventory = not harvest_active and piece.not_in_inventory is True
            if route_not_in_inventory:
                self.logger.info(
                    f"Positioning: piece {piece.uuid} ({piece.part_id}) not in active "
                    f"inventory — routing within not-in-inventory bins as {category_id}"
                )
            if not harvest_active:
                address, _ = self._findOrAssignBinForCategory(
                    category_id, not_in_inventory=route_not_in_inventory
                )
            if address is None and self._servo_bus_pause_enqueued:
                # Fatal: the servo bus is offline, so every layer is
                # unusable. ``_findOrAssignBinForCategory`` already set
                # the red banner and enqueued a pause — do NOT fall
                # through to the passthrough path, which would silently
                # send this piece to the discard bucket.
                return DistributionState.IDLE
            if address is None:
                # MISC is the intentional reject/default category. It should
                # pass through to the bottom tray without claiming a real bin
                # and without raising the operator no-bin incident.
                if (
                    category_id != MISC_CATEGORY
                    and not bool(getattr(piece, "reject_on_routing_failure", False))
                    and not self._consumeNoBinPassthroughApproval(piece)
                    and self._raiseNoBinAvailableIncident(piece, category_id)
                ):
                    self._setOccupancyState("positioning.no_bin_incident")
                    return DistributionState.IDLE
                # Pass-through: no bin available for this category. Open
                # every usable layer door so the piece falls straight
                # through to the bottom tray. Finalize the piece record
                # via SENDING so stats/events stay consistent.
                self.logger.warning(
                    f"Positioning: no bin for category {category_id} — passthrough to bottom"
                )
                self._raiseBinsFullAlert(category_id)
                self._openAllDoorsForPassthrough()
                piece.stage = PieceStage.distributing
                piece.distributing_at = time.time()
                piece.distribution_target_selected_at = piece.distributing_at
                piece.category_id = category_id
                piece.destination_bin = None
                piece.updated_at = time.time()
                self._piece = piece
                self.event_queue.put(knownObjectToEvent(piece))
                self._setOccupancyState("positioning.passthrough_no_bin")
                return DistributionState.READY

            if self._exceedsLayerMaxDimension(piece, address.layer_index):
                # The piece fits a real bin by category, but is physically too
                # large for that bin's layer. Reroute it to the misc bottom bin
                # (center-of-chute passthrough) and mark why. A live Harvest
                # allocation is confirmed as an exception after the drop.
                layer_max = self._layerMaxDimensionMm(address.layer_index)
                self.logger.info(
                    f"Positioning: piece {piece.uuid} ({piece.max_dimension_mm}mm) exceeds "
                    f"layer {address.layer_index} limit ({layer_max}mm) — passthrough to misc bottom bin"
                )
                self._clearBinsFullAlertIfOwned()
                self._clearChuteJamAlertIfOwned()
                self._openAllDoorsForPassthrough()
                piece.stage = PieceStage.distributing
                piece.distributing_at = time.time()
                piece.distribution_target_selected_at = piece.distributing_at
                piece.category_id = MISC_CATEGORY
                piece.destination_bin = None
                piece.too_big_for_layer = True
                piece.intended_layer_index = address.layer_index
                if harvest_active:
                    piece.harvest_exception = True
                piece.updated_at = time.time()
                self._piece = piece
                self.event_queue.put(knownObjectToEvent(piece))
                self._setOccupancyState("positioning.passthrough_too_big_for_layer")
                return DistributionState.READY

            self._clearBinsFullAlertIfOwned()
            self._clearChuteJamAlertIfOwned()
            self.logger.info(
                f"Positioning: moving to bin at layer={address.layer_index}, section={address.section_index}, bin={address.bin_index}"
            )
            self._target_address = address
            self._piece = piece
            if not self._startDoorAndChutePositioning(address.layer_index):
                self.logger.warning(
                    f"Positioning: layer {address.layer_index} is unavailable for distribution, retrying with remaining layers"
                )
                # If every layer is now blocked AND the root cause is that
                # every configured servo is offline, this is the fatal
                # servo-bus case: the controller can no longer dispense
                # anything. Pause + red banner. Fall back to the generic
                # chute-jam alert only if blocks came from other causes
                # (e.g. transient close-timeouts on a live bus).
                usable_count = sum(
                    1
                    for li, layer in enumerate(self.layout.layers)
                    if getattr(layer, "enabled", True) and li not in self._blocked_layers
                )
                if usable_count == 0:
                    if self._allEnabledLayersServoOffline():
                        self._raiseServoBusOfflineAlert(
                            "no layer servo responded during door selection"
                        )
                    else:
                        self._raiseChuteJamAlert(
                            "all layer servos are offline"
                        )
                return DistributionState.IDLE

            piece.stage = PieceStage.distributing
            piece.distributing_at = time.time()
            piece.distribution_target_selected_at = piece.distributing_at
            piece.category_id = category_id
            piece.destination_bin = (
                address.layer_index,
                address.section_index,
                address.bin_index,
            )
            piece.updated_at = time.time()
            self.event_queue.put(knownObjectToEvent(piece))

            init_ms = (self._moving_started_at - self._state_entered_at) * 1000
            if self.gc.disable_servos:
                self.logger.info(f"Positioning: init phase took {init_ms:.0f}ms, now waiting for chute")
            else:
                self.logger.info(f"Positioning: init phase took {init_ms:.0f}ms, now waiting for servo+chute")
            return None

        if self._phase == "moving":
            self._setOccupancyState("positioning.wait_servo_and_chute_motion")
            chute_stopped = self.chute.stepper.stopped
            moving_servo_indices = self._movingDoorServoIndices()
            servo_stopped = not moving_servo_indices
            if not chute_stopped or not servo_stopped:
                # Jam detection: if we've been "waiting for movement" for
                # way longer than the estimated move time, something is
                # physically stuck. Raise hard alert + pause.
                elapsed_ms = (now - self._moving_started_at) * 1000
                budget_ms = max(
                    CHUTE_MOVE_TIMEOUT_MS,
                    int(self._chute_move_estimated_ms * CHUTE_MOVE_TIMEOUT_MULTIPLIER),
                )
                if elapsed_ms > budget_ms:
                    stuck = []
                    if not chute_stopped:
                        stuck.append("chute stepper did not stop")
                    if not servo_stopped:
                        stuck.append(
                            "layer servo flap(s) did not stop: "
                            + ", ".join(str(i) for i in moving_servo_indices)
                        )
                    self._raiseChuteJamAlert(
                        f"{' and '.join(stuck)} after {elapsed_ms:.0f}ms (budget {budget_ms}ms)"
                    )
                    # Abort this sortation; remain in POSITIONING so the
                    # pause event handled by the coordinator can take
                    # over. Returning IDLE here would let the next piece
                    # start a new move on top of the stuck one.
                    return None
                return None
            self.shared.set_chute_motion(False, target_bin=self._target_address)
            if self._piece is not None and self._piece.distribution_positioned_at is None:
                self._piece.distribution_positioned_at = time.time()
            move_ms = (now - self._moving_started_at) * 1000
            total_ms = (now - self._state_entered_at) * 1000
            if self.gc.disable_servos:
                self.logger.info(f"Positioning: complete (chute={move_ms:.0f}ms, total={total_ms:.0f}ms)")
            else:
                self.logger.info(f"Positioning: complete (servo+chute={move_ms:.0f}ms, total={total_ms:.0f}ms)")
            return DistributionState.READY

        return None

    def _reserveHarvestRoute(self, piece):
        """Return an exact activated destination, None, or False on a fatal gate."""

        try:
            from project_harvest_runtime import reserve_piece

            allocation = reserve_piece(self.gc, piece)
        except Exception as exc:
            self.logger.exception("Positioning: Harvest allocation failed")
            self._raiseHarvestRuntimeAlert(str(exc))
            return False
        if allocation is None:
            return None
        destination = allocation.get("destination")
        if not isinstance(destination, dict):
            self._raiseHarvestRuntimeAlert("the active allocation has no destination")
            return False
        try:
            address = BinAddress(
                int(destination["layer_index"]),
                int(destination["section_index"]),
                int(destination["bin_index"]),
            )
            target_bin = self.layout.layers[address.layer_index].sections[
                address.section_index
            ].bins[address.bin_index]
        except (KeyError, IndexError, TypeError, ValueError):
            self._raiseHarvestRuntimeAlert("the activated bin no longer exists")
            return False
        category_id = str(destination.get("category_id") or "")
        if category_id not in target_bin.category_ids:
            self._raiseHarvestRuntimeAlert(
                "the activated bin assignment changed after green light"
            )
            return False
        from local_state import get_current_bin_piece_counts

        count = get_current_bin_piece_counts().get(
            (address.layer_index, address.section_index, address.bin_index), 0
        )
        max_pieces = getattr(self.layout.layers[address.layer_index], "max_pieces_per_bin", None)
        if max_pieces is not None and count >= max_pieces:
            self._raiseHarvestRuntimeAlert(
                f"activated destination {destination.get('bin_id')} is full"
            )
            return False
        if not self.chute.isBinReachable(address):
            self._raiseHarvestRuntimeAlert(
                f"activated destination {destination.get('bin_id')} is unreachable"
            )
            return False

        piece.harvest_project_id = allocation["project_id"]
        piece.harvest_activation_id = allocation["activation_id"]
        piece.harvest_allocation_id = allocation["allocation_id"]
        piece.harvest_group_id = allocation["group_id"]
        piece.harvest_group_label = str(destination.get("group_label") or allocation["group_id"])
        piece.harvest_exception = bool(allocation.get("exception"))
        self.logger.info(
            "Positioning: Harvest piece %s reserved for %s in %s"
            % (piece.uuid, piece.harvest_group_label, destination.get("bin_id"))
        )
        return address, category_id

    def _raiseHarvestRuntimeAlert(self, detail: str) -> None:
        message = f"{HARVEST_RUNTIME_ALERT_PREFIX}: {detail}"
        try:
            with shared_state.hardware_lifecycle_lock:
                shared_state.setHardwareStatus(error=message)
        except Exception:
            pass
        if self._harvest_pause_enqueued:
            return
        command_queue = shared_state.command_queue
        if command_queue is None:
            self.logger.error(
                "Positioning: cannot enqueue Harvest safety pause because the controller "
                "command queue is unavailable"
            )
            return
        try:
            command_queue.put_nowait(
                PauseCommandEvent(tag="pause", data=PauseCommandData())
            )
            self._harvest_pause_enqueued = True
        except Exception:
            self.logger.exception("Positioning: failed to enqueue Harvest safety pause")

    def _startChuteMove(self) -> None:
        assert self._target_address is not None
        self.shared.set_chute_motion(True, target_bin=self._target_address)
        if self._piece is not None and self._piece.distribution_motion_started_at is None:
            self._piece.distribution_motion_started_at = time.time()
        self._moving_started_at = time.monotonic()
        estimated_ms = self.chute.moveToBin(self._target_address)
        self._chute_move_estimated_ms = int(estimated_ms)
        self.logger.info(
            f"Positioning: chute move started (est_ms={estimated_ms})"
        )
        self._phase = "moving"

    def cleanup(self) -> None:
        super().cleanup()
        target_address = self._target_address
        self._phase = "init"
        self._target_address = None
        self._door_servo_index = None
        self._door_servo_indices = []
        self._state_entered_at = 0.0
        self._moving_started_at = 0.0
        self._piece = None
        self.shared.set_chute_motion(False, target_bin=target_address)

    def _layerMaxDimensionMm(self, layer_index: int) -> Optional[float]:
        layers = getattr(self.layout, "layers", [])
        if 0 <= layer_index < len(layers):
            value = getattr(layers[layer_index], "max_dimension_mm", None)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
        return None

    def _exceedsLayerMaxDimension(self, piece, layer_index: int) -> bool:
        layer_max = self._layerMaxDimensionMm(layer_index)
        if layer_max is None:
            return False
        piece_max = piece.max_dimension_mm
        if not isinstance(piece_max, (int, float)):
            return False
        return float(piece_max) > layer_max

    def _isLayerUsable(self, layer_index: int) -> bool:
        """Check whether this layer is currently usable for a sort move.

        Historical behavior added a layer to ``_blocked_layers`` on the
        first transient servo hiccup and *never* removed it, which in
        practice meant one flaky heartbeat permanently disabled the
        layer until a restart. Now we always check the servo's live
        ``available`` state; ``_blocked_layers`` still acts as a fast
        no-op gate but gets cleared automatically once the servo reports
        healthy again.
        """
        if self.gc.disable_servos:
            return True
        if layer_index >= len(self.irl.servos):
            self._markLayerUnavailable(layer_index, "no servo configured for this layer")
            return False
        servo_available = bool(getattr(self.irl.servos[layer_index], "available", True))
        if not servo_available:
            self._servo_offline_layers.add(layer_index)
            self._markLayerUnavailable(layer_index, "its servo backend is offline")
            return False
        if not bool(getattr(self.irl.servos[layer_index], "is_calibrated", True)):
            # Uncalibrated PWM servos are skipped so pieces are never routed to a
            # door that cannot safely move. Logged at debug (not warning) because
            # this state persists until the operator calibrates the layer, and
            # _blocked_layers may reset per-step — a warning would spam the log.
            self.logger.debug(
                f"Positioning: skipping layer {layer_index} — servo not calibrated"
            )
            return False
        if layer_index in self._blocked_layers:
            # Servo has recovered — clear the cached block so positioning
            # starts considering this layer again.
            self._blocked_layers.discard(layer_index)
            self._servo_offline_layers.discard(layer_index)
            self.logger.info(
                f"Positioning: layer {layer_index} servo is back online, re-enabling"
            )
        # A healthy servo read means the Waveshare bus is talking again, so
        # clear any stale servo-bus banner regardless of whether the layer
        # was previously in the local block list. (The block list is
        # per-step and may have just been reset at the top of ``step``.)
        self._clearServoBusOfflineAlertIfOwned()
        return True

    def _markLayerUnavailable(self, layer_index: int, reason: str) -> None:
        if layer_index in self._blocked_layers:
            return
        self._blocked_layers.add(layer_index)
        self.logger.warning(
            f"Positioning: disabling layer {layer_index} temporarily because {reason}"
        )

    def _movingDoorServoIndices(self) -> list[int]:
        moving: list[int] = []
        for layer_index in self._door_servo_indices:
            try:
                if not self.irl.servos[layer_index].stopped:
                    moving.append(layer_index)
            except Exception as exc:
                self._markLayerUnavailable(
                    layer_index,
                    f"servo stop check failed: {exc}",
                )
        return moving

    def _raiseBinsFullAlert(self, category_id: str) -> None:
        return

    def _consumeNoBinPassthroughApproval(self, piece) -> bool:
        consumer = getattr(shared_state, "consumeDistributionNoBinPassthrough", None)
        if not callable(consumer):
            return False
        try:
            return bool(consumer(getattr(piece, "uuid", None)))
        except Exception:
            return False

    def _raiseNoBinAvailableIncident(self, piece, category_id: str) -> bool:
        """Publish no-bin as an explicit operator incident.

        When this incident kind is disabled, the legacy passthrough path below
        remains available as an intentional operating choice.
        """
        piece_uuid = str(getattr(piece, "uuid", "") or "")
        return self._publishDistributionIncident(
            DISTRIBUTION_NO_BIN_AVAILABLE_INCIDENT_KIND,
            detail=f"No bin is available for category {category_id}.",
            severity="warning",
            role="distribution_no_bin",
            channel_label="Distribution",
            category_id=str(category_id),
            piece_uuid=piece_uuid,
            piece_short=piece_uuid[:8],
            part_id=getattr(piece, "part_id", None),
            color_id=getattr(piece, "color_id", None),
            resolution="operator_assign_bin_or_clear_to_approve_one_shot_passthrough",
            operator_message=(
                "No matching bin is available. Assign a bin, free capacity, or clear the incident to pass this piece through once."
            ),
        )

    def _allEnabledLayersServoOffline(self) -> bool:
        """True iff every enabled layer's servo is currently flagged as
        offline. Used to separate the fatal servo-bus case from a soft
        "one layer jammed" case.
        """
        if self.gc.disable_servos:
            return False
        enabled_indices = [
            li
            for li, layer in enumerate(self.layout.layers)
            if getattr(layer, "enabled", True)
        ]
        if not enabled_indices:
            return False
        for li in enabled_indices:
            if li >= len(self.irl.servos):
                # Treat missing entries as offline so a half-configured
                # machine trips the alert too.
                continue
            if bool(getattr(self.irl.servos[li], "available", True)):
                return False
        return True

    def _raiseServoBusOfflineAlert(self, detail: str) -> None:
        """Fatal: the Waveshare bus is unreachable so no piece can be
        routed. Sets the red banner once, records the fault timestamp in
        runtime_stats, and pauses the controller. Cleared only when a
        servo comes back online via ``_isLayerUsable``.
        """
        self._publishDistributionIncident(
            DISTRIBUTION_SERVO_BUS_OFFLINE_INCIDENT_KIND,
            detail=detail,
            severity="critical",
            role="distribution_servo_bus",
            channel_label="Servo Bus",
            offline_layers=sorted(self._servo_offline_layers),
        )
        if self._servo_bus_pause_enqueued:
            return
        self._servo_bus_pause_enqueued = True
        message = (
            f"{SERVO_BUS_ALERT_PREFIX} — {detail}. "
            "Check Waveshare USB + power, then press Resume."
        )
        self.logger.error(message)
        try:
            self.gc.profiler.hit("distribution.servo_bus_offline")
            self.gc.runtime_stats.observeBlockedReason(
                "distribution", "servo_bus_offline"
            )
            self.gc.runtime_stats.setServoBusOffline()
        except Exception:
            pass
        try:
            with shared_state.hardware_lifecycle_lock:
                shared_state.setHardwareStatus(error=message)
        except Exception:
            pass
        try:
            if shared_state.command_queue is not None:
                shared_state.command_queue.put(
                    PauseCommandEvent(tag="pause", data=PauseCommandData())
                )
        except Exception:
            pass

    def _targetAddressPayload(self) -> dict | None:
        if self._target_address is None:
            return None
        return {
            "layer_index": int(self._target_address.layer_index),
            "section_index": int(self._target_address.section_index),
            "bin_index": int(self._target_address.bin_index),
        }

    def _publishDistributionIncident(
        self,
        kind: str,
        *,
        detail: str,
        severity: str,
        role: str,
        channel_label: str,
        **extra,
    ) -> bool:
        if _incidentHandlingOff(kind):
            return False
        runtime_stats = getattr(self.gc, "runtime_stats", None)
        if runtime_stats is None or not hasattr(runtime_stats, "setActiveIncident"):
            return False
        active = None
        if hasattr(runtime_stats, "activeIncident"):
            try:
                active = runtime_stats.activeIncident()
            except Exception:
                active = None
        if isinstance(active, dict):
            if active.get("kind") == kind:
                return True
            return True
        incident = {
            "kind": kind,
            "severity": severity,
            "status": "waiting_for_operator",
            "awaiting_operator": True,
            "scope": "distribution",
            "channel": "distribution",
            "role": role,
            "channel_label": channel_label,
            "triggered_at": time.time(),
            "detail": detail,
        }
        incident.update(extra)
        runtime_stats.setActiveIncident(incident)
        return True

    def _clearDistributionIncident(self, kind: str) -> None:
        runtime_stats = getattr(self.gc, "runtime_stats", None)
        if runtime_stats is None or not hasattr(runtime_stats, "activeIncident"):
            return
        try:
            active = runtime_stats.activeIncident()
        except Exception:
            active = None
        if (
            isinstance(active, dict)
            and active.get("kind") == kind
            and hasattr(runtime_stats, "clearActiveIncident")
        ):
            runtime_stats.clearActiveIncident(kind=kind)

    def _clearServoBusOfflineAlertIfOwned(self) -> None:
        try:
            with shared_state.hardware_lifecycle_lock:
                err = shared_state.hardware_error
                if isinstance(err, str) and err.startswith(SERVO_BUS_ALERT_PREFIX):
                    shared_state.setHardwareStatus(clear_error=True)
        except Exception:
            pass
        self._servo_bus_pause_enqueued = False
        self._clearDistributionIncident(DISTRIBUTION_SERVO_BUS_OFFLINE_INCIDENT_KIND)
        try:
            self.gc.runtime_stats.clearServoBusOffline()
        except Exception:
            pass

    def _raiseChuteJamAlert(self, detail: str) -> None:
        """Hard alert: chute / servo can't physically move. Raises the red
        banner *and* enqueues a pause so the operator has to intervene.
        """
        elapsed_ms = int(max(0.0, time.monotonic() - self._moving_started_at) * 1000.0)
        self._publishDistributionIncident(
            DISTRIBUTION_CHUTE_JAM_INCIDENT_KIND,
            detail=detail,
            severity="critical",
            role="distribution_chute",
            channel_label="Distribution Chute",
            elapsed_ms=elapsed_ms,
            estimated_ms=int(self._chute_move_estimated_ms),
            target_address=self._targetAddressPayload(),
        )
        if self._jam_pause_enqueued:
            return
        self._jam_pause_enqueued = True
        message = (
            f"{CHUTE_JAM_ALERT_PREFIX}: {detail}. "
            "Clear any piece stuck in the chute or on the distribution tray, "
            "make sure the servo flap can move freely, then press play."
        )
        self.logger.error(message)
        try:
            self.gc.profiler.hit("distribution.chute_jam")
            self.gc.runtime_stats.observeBlockedReason("distribution", "chute_jam")
        except Exception:
            pass
        try:
            with shared_state.hardware_lifecycle_lock:
                shared_state.setHardwareStatus(error=message)
        except Exception:
            pass
        try:
            if shared_state.command_queue is not None:
                shared_state.command_queue.put(
                    PauseCommandEvent(tag="pause", data=PauseCommandData())
                )
        except Exception:
            pass

    def _clearChuteJamAlertIfOwned(self) -> None:
        try:
            with shared_state.hardware_lifecycle_lock:
                err = shared_state.hardware_error
                if isinstance(err, str) and err.startswith(CHUTE_JAM_ALERT_PREFIX):
                    shared_state.setHardwareStatus(clear_error=True)
        except Exception:
            pass
        self._jam_pause_enqueued = False
        self._clearDistributionIncident(DISTRIBUTION_CHUTE_JAM_INCIDENT_KIND)

    def _clearBinsFullAlertIfOwned(self) -> None:
        try:
            with shared_state.hardware_lifecycle_lock:
                err = shared_state.hardware_error
                if isinstance(err, str) and (
                    err.startswith(BINS_FULL_ALERT_PREFIX)
                    or err.startswith(MISC_PASSTHROUGH_ALERT_PREFIX)
                ):
                    shared_state.setHardwareStatus(clear_error=True)
        except Exception:
            pass

    def _openAllDoorsForPassthrough(self) -> None:
        """Open every usable layer door so a piece with no assigned bin
        falls straight through to the bottom tray. A follow-up
        ``_selectDoor`` on the next piece will re-close the appropriate
        layer.
        """
        if self.gc.disable_servos:
            return
        for i, servo in enumerate(self.irl.servos):
            if not self._isLayerUsable(i):
                continue
            try:
                # Re-issue every open command. A stale shadow state must not
                # make an automatic reject trust a door that is physically
                # closed after a dropped write or power interruption.
                if hasattr(servo, "apply_open_speed"):
                    servo.apply_open_speed()
                servo.open()
            except Exception as exc:
                self._markLayerUnavailable(
                    i,
                    f"opening for passthrough failed: {exc}",
                )

    def _startDoorAndChutePositioning(self, target_layer_index: int) -> bool:
        """Start independent distribution motions without serializing them.

        The target door is the only command that must succeed before the chute
        can safely start. Once it is accepted, the chute and the remaining door
        motions can run concurrently; POSITIONING still waits for every issued
        motion before opening the distribution gate.
        """
        if self.gc.disable_servos:
            self._startChuteMove()
            return True
        if not self._isLayerUsable(target_layer_index):
            self._markLayerUnavailable(target_layer_index, "the target servo is unavailable")
            return False

        target_servo = self.irl.servos[target_layer_index]

        try:
            if hasattr(target_servo, "apply_close_speed"):
                target_servo.apply_close_speed()
            target_servo.close()
        except Exception as exc:
            self._markLayerUnavailable(
                target_layer_index,
                f"closing target servo failed: {exc}",
            )
            return False

        self._door_servo_index = target_layer_index
        self._door_servo_indices = [target_layer_index]
        self._startChuteMove()

        # Never trust shadow state: re-issue the full door configuration before
        # every dispense. These independent moves run while the chute travels.
        opened_layers: list[int] = []
        for i, servo in enumerate(self.irl.servos):
            if i == target_layer_index or not self._isLayerUsable(i):
                continue
            try:
                if hasattr(servo, "apply_open_speed"):
                    servo.apply_open_speed()
                servo.open()
                self._door_servo_indices.append(i)
                opened_layers.append(i)
            except Exception as exc:
                self._markLayerUnavailable(
                    i,
                    f"opening parked servo failed: {exc}",
                )

        # Single line capturing the full intended door state for this dispense,
        # so a wrong-layer piece can be traced to exactly what the controller
        # commanded (vs. what the flaps physically did, which is open-loop).
        self.logger.info(
            f"Door config: closed layer-{target_layer_index} (target), "
            f"opened parked layers {opened_layers}"
        )
        return True

    def _findOrAssignBinForCategory(
        self, category_id: str, not_in_inventory: bool = False
    ) -> tuple[Optional[BinAddress], bool]:
        # ``not_in_inventory`` selects which bin pool to search. Pieces absent
        # from the active .bsx route only among bins flagged not-in-inventory;
        # everyone else routes only among the normal bins. The two pools never
        # mix. Within the not-in-inventory pool, overlap (multi-category) is
        # always allowed as the last resort ("if we run out, overlap them").
        from local_state import get_current_bin_piece_counts

        piece_counts = get_current_bin_piece_counts()
        # A category may be assigned to more than one bin (the same category_id
        # appears in several bins' category_ids). When that happens we spread the
        # load by picking uniformly at random among every reachable, non-full bin
        # holding it — so N bins each take ~1/N of the pieces. Collected across
        # the whole scan; the pick happens after the loop.
        matching_candidates: list[BinAddress] = []
        first_unassigned: Optional[tuple[BinAddress, "Bin"]] = None
        # Least-loaded shared-bin candidate, used only when every bin is
        # already assigned and multi-category bins are enabled: (num_categories,
        # piece_count, address, bin). Picking the bin with the fewest categories
        # (tie-break: fewest pieces) spreads new categories evenly.
        best_combine: Optional[tuple[int, int, BinAddress, "Bin"]] = None
        has_usable_layers = False

        # Debug trace — categorizes every bin we looked at and why it was
        # skipped. Dumped below when the whole search comes back empty.
        skipped: list[str] = []
        enabled_layers = 0
        blocked_layer_idxs: list[int] = []
        unreachable_bins = 0
        full_bins = 0
        bins_with_cats = 0

        for layer_idx, layer in enumerate(self.layout.layers):
            if not getattr(layer, "enabled", True):
                skipped.append(f"layer{layer_idx}=disabled")
                continue
            enabled_layers += 1
            if not self._isLayerUsable(layer_idx):
                blocked_layer_idxs.append(layer_idx)
                continue

            has_usable_layers = True
            max_per_bin = getattr(layer, "max_pieces_per_bin", None)
            for section_idx, section in enumerate(layer.sections):
                if not getattr(section, "enabled", True):
                    skipped.append(f"layer{layer_idx}.section{section_idx}=disabled")
                    continue
                for bin_idx, b in enumerate(section.bins):
                    if bool(b.not_in_inventory) != not_in_inventory:
                        continue
                    address = BinAddress(layer_idx, section_idx, bin_idx)
                    if not self.chute.isBinReachable(address):
                        unreachable_bins += 1
                        continue
                    count = piece_counts.get((layer_idx, section_idx, bin_idx), 0)
                    is_full = max_per_bin is not None and count >= max_per_bin
                    if (
                        category_id != MISC_CATEGORY
                        and category_id in b.category_ids
                        and not is_full
                    ):
                        matching_candidates.append(address)
                        continue
                    if b.category_ids:
                        bins_with_cats += 1
                    if is_full:
                        full_bins += 1
                    if not b.category_ids and first_unassigned is None:
                        first_unassigned = (address, b)
                    if (
                        category_id != MISC_CATEGORY
                        and b.category_ids
                        and not is_full
                        and MISC_CATEGORY not in b.category_ids
                    ):
                        candidate = (len(b.category_ids), count, address, b)
                        if best_combine is None or candidate[:2] < best_combine[:2]:
                            best_combine = candidate

        if not has_usable_layers:
            self.logger.warning(
                f"Positioning: no usable storage layers (enabled={enabled_layers}, "
                f"blocked={blocked_layer_idxs}, skipped={skipped})"
            )
            # If every enabled layer is blocked because its servo backend is
            # offline (vs. e.g. an occasional close-timeout on a live bus),
            # this is the fatal case — raise the red banner + pause. We
            # raise here rather than inside ``_selectDoor`` because this
            # path catches bin-lookup before we ever commit to a layer.
            if enabled_layers > 0 and self._allEnabledLayersServoOffline():
                self._raiseServoBusOfflineAlert(
                    "every layer servo is unreachable"
                )
            return None, False

        # Category is assigned to one or more bins: route to one of them. With a
        # single bin this is just that bin; with several it is a uniform random
        # choice, so a category spanning N bins fills them at an even ~1/N rate.
        if matching_candidates:
            return random.choice(matching_candidates), False

        # MISC must never claim or use a real bin. The discard bucket below
        # the chute (rendered in the UI as the virtual Discard Bin) is what
        # catches misc passthrough; treating MISC as a physical bin category
        # silently swaps that behavior.
        if first_unassigned is not None and category_id != MISC_CATEGORY:
            address, b = first_unassigned
            b.category_ids = [category_id]
            setBinCategories(extractCategories(self.layout))
            self.logger.info(
                f"Positioning: assigned category {category_id} to bin at layer={address.layer_index}, section={address.section_index}, bin={address.bin_index}"
            )
            return address, True

        # Every bin is already assigned and none is empty. If the operator
        # enabled multi-category bins, keep sorting by combining this category
        # into the least-loaded existing bin rather than dumping to the discard
        # passthrough. Checked here (rather than per-piece up top) so it only
        # costs a TOML read once bins are actually exhausted.
        if (
            category_id != MISC_CATEGORY
            and best_combine is not None
            and (not_in_inventory or _allowMultiCategoryBins())
        ):
            _, _, address, b = best_combine
            b.category_ids.append(category_id)
            setBinCategories(extractCategories(self.layout))
            self.logger.info(
                f"Positioning: combined category {category_id} into shared bin at "
                f"layer={address.layer_index}, section={address.section_index}, "
                f"bin={address.bin_index} (now holds {b.category_ids})"
            )
            return address, True

        if category_id != MISC_CATEGORY and not not_in_inventory:
            # No bin slot available for this category; fall back to MISC,
            # which will only succeed if the operator pre-assigned a bin
            # to MISC. Otherwise it returns None and the piece passes
            # through to the discard bucket. Not-in-inventory pieces never fall
            # back to a normal MISC bin — they stay in their pool or pass through.
            return self._findOrAssignBinForCategory(MISC_CATEGORY)

        self.logger.info(
            f"Positioning: MISC has no assigned bin — passthrough to discard bucket "
            f"(enabled_layers={enabled_layers}, blocked={blocked_layer_idxs}, "
            f"unreachable_bins={unreachable_bins}, full_bins={full_bins}, "
            f"bins_with_cats={bins_with_cats}, skipped={skipped})"
        )
        return None, False
