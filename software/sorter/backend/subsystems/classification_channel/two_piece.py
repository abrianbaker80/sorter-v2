import math
import time
from enum import Enum
from typing import Optional

from defs.known_object import (
    ClassificationStatus,
    KnownObject,
    PieceStage,
    RecognitionImage,
)

from . import crop_quality
from .simple_state_machine_rev01.base import Rev01BaseState
from .simple_state_machine_rev01.channel_clear import ChannelClearResult
from .incidents import CLASSIFICATION_TRACK_LOST_INCIDENT_KIND
from .simple_state_machine_rev01.constants import C4_TRAVEL_SIGN
from .simple_state_machine_rev01.context import SimpleStateMachineRev01Context
from .five_sector_platter import C4FiveSectorPlatter
from ..distribution.flap_path import flap_path_settled

LOG_TAG = "[C4-2PIECE]"

# perception.arcs._region_lookup / PieceObservation.zone_code values. The
# classification channel is a rotating platter viewed from above; pieces travel
# FORWARD (one way, never reversed) through these zones in this order:
#   DROP  -> PRECISE (the holding region) -> EXIT (the fall-off)
# Anything that has LEFT the drop zone (PRECISE, EXIT, or the unnamed gap NONE
# between them) is "forward" and part of the processing queue.
_ZONE_NONE = 0
_ZONE_DROP = 1
# (2 = exit / the fall-off, 3 = precise / the holding region; we only branch on
# DROP vs not-DROP — "left the drop zone" is what matters for the queue.)

# Only unowned detector noise may expire. Captured/placed pieces retain their
# identity until an exit-confirmed handoff or explicit controller teardown.
_TRACK_GONE_RETIRE_S = 0.7
# Absence is supporting evidence only after observing the commanded target in
# the exit, with a settled motor and at least two distinct subsequent frames.
_EJECT_GONE_CONFIRM_S = 0.35
_MAX_FRAME_AGE_S = 1.0
# New tracker identities enter through a short, stopped observation window.
# The first plausible pair accepts immediately; after a transient continuity
# failure require two stable pairs, within at most eight additional fresh frames.
_NEW_TRACK_STABLE_PAIRS = 2
_NEW_TRACK_CONFIRMATION_FRAMES = 8
# A forward (already left the drop zone) piece that was NEVER captured/classified
# — a stray, a detector-churn leftover, or the trailing piece of a multi-drop
# that skipped the drop zone — is routed to misc after this long so it can be
# ejected instead of deadlocking as an un-shippable head.
_STRAY_MISC_S = 4.0
# Fixed forward nudge (output deg) used while STAGING once the leading piece has
# already reached/passed precise but the drop zone still isn't clear — keeps
# pushing the clump out of the drop zone without a gap to size against.
_STAGE_STEP_DEG = 25.0

# Safety ceilings so a move that never resolves can't wedge the machine forever.
_EJECT_TIMEOUT_S = 15.0
_STAGE_TIMEOUT_S = 15.0

# Stall-watchdog progress signal: a tracked piece's gap-to-exit must change by
# more than this (output deg) to count as the piece actually moving. Well above
# per-frame detection jitter on a stationary piece, well below any real nudge.
_PROGRESS_MOVE_DEG = 5.0


class _Phase(Enum):
    # Platter STOPPED. Observe, photograph the drop-zone piece, classify, and aim
    # the chute for the head piece. The only place we accept a new piece.
    WAITING = "waiting"
    # Rotating the head off the fall-off, then verifying its exit and absence.
    EJECTING = "ejecting"
    # Rotating the clump forward until the drop zone is clear (the new piece, plus
    # any multi-drop siblings, have all left the drop zone).
    STAGING = "staging"


class _TrackedPiece:
    """One physical piece on the channel, keyed by its perception track id. Owns
    a private capture/classify worker (its own KnownObject + burst context) so two
    pieces never share classification state."""

    def __init__(self, track_id: int, worker: Rev01BaseState, now: float) -> None:
        self.track_id = track_id
        self.worker = worker
        self.zone = _ZONE_NONE
        self.bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
        self.gap_to_exit: Optional[float] = None
        self.clearance_to_exit: Optional[float] = None
        self.motor_position: Optional[int] = None
        # Gap at the last time this piece was credited with real movement (the
        # stall watchdog's "has it moved substantially" reference point).
        self.progress_gap: Optional[float] = None
        self.created_at = now
        self.last_seen = now
        self.visible = True
        self.absent_since_ts: Optional[float] = None
        self.absent_frames = 0
        self.exit_seen_ts: Optional[float] = None
        self.settled_absent_since: Optional[float] = None
        self.settled_absent_frames = 0
        # Burst capture (drop zone only) has finished -> safe to rotate the piece
        # out of the drop zone.
        self.capture_done = False
        # Classification result has been written onto the KnownObject.
        self.result_applied = False
        # Handed to distribution (chute is aiming / aimed for it).
        self.placed = False
        # Two+ pieces landed in the drop zone at once -> can't classify reliably,
        # route to the misc bin. multi_drop_group ties the clump's distinct track
        # ids together as one logical multi-drop (None when not a multi-drop).
        self.double_feed = False
        self.multi_drop_group: Optional[int] = None
        # A just-created DROP identity is not yet a physical owner. Its first
        # observation is only a baseline; later distinct frames must support the
        # same tracker generation/ID before capture or motion can use it.
        self.continuity_confirmed = True
        self.continuity_suspect = False
        self.continuity_stable_pairs = 0
        self.continuity_generation: Optional[str] = None
        self.continuity_zone: Optional[int] = None
        self.continuity_last_ts: Optional[float] = None

    @property
    def known_object(self) -> Optional[KnownObject]:
        return self.worker.ctx.known_object


class TwoPieceClassificationChannel(Rev01BaseState):
    """Hold up to two pieces on the classification channel, one cycle ahead of
    the chute: a HEAD being classified + aimed + ejected, and a fresh piece
    captured in the drop zone. The platter only ever turns FORWARD (clockwise);
    no move is reversed.

    Perception IDs associate observations with retained physical owners. A
    missing ID alone never establishes departure or frees an owned piece.

    The channel is treated as an ORDERED QUEUE (most-forward = head), not a fixed
    "one in precise, one in drop" pair — so a multi-drop clump or a stray that
    lands between zones is always part of the queue and never stranded.

    State machine (platter-level; per-piece classification runs concurrently):

      WAITING (stopped) --------------------------------------------------+
        - photograph the drop piece (drop zone only), classify off-thread |
        - aim the chute for the head once it's classified                 |
        - feeder may add a piece only here (drop clear + stopped)         |
        - ROTATE when there is a captured drop piece AND                  |
            (no head            -> STAGING, or                            |
             head is ready      -> EJECTING)                              |
                                                                          |
      EJECTING (moving) -- push the head off the fall-off                 |
        - exit observed + settled absence -> commit to distribution     |
          -> STAGING                                                      |
                                                                          |
      STAGING (moving) -- advance until the DROP ZONE IS CLEAR (whole     |
        clump leaves drop) -> WAITING ------------------------------------+

    "Ready" for the head = classified AND the distribution chute is aimed for it.
    A multi-drop's pieces stay distinct but share a ``multi_drop_group`` id and
    all route to misc, draining one per cycle.

    The single-piece SIMPLE_STATE_MACHINE_REV01 path is untouched.
    """

    def __init__(
        self,
        irl,
        irl_config,
        gc,
        shared,
        transport,
        vision,
        event_queue,
        context: SimpleStateMachineRev01Context,
    ):
        super().__init__(
            irl, irl_config, gc, shared, transport, vision, event_queue, context
        )
        self._deps = (irl, irl_config, gc, shared, transport, vision, event_queue)
        self._pieces: dict[int, _TrackedPiece] = {}
        self._phase = _Phase.WAITING
        self._eject_target: Optional[_TrackedPiece] = None
        self._stage_target: Optional[_TrackedPiece] = None
        self._phase_started_at = 0.0
        self._last_observation_ts = 0.0
        self._eject_move_accepted = False
        self._completed_ids: set[int] = set()
        self._safety_hold_reason: Optional[str] = None
        self._automatic_reject_requested = False
        self._hold_stop_started: Optional[float] = None
        self._hold_stop_last_attempt = float("-inf")
        self._hold_stop_attempts = 0
        self._hold_stop_verified = False
        self._hold_stop_failed = False
        self._hold_stop_error: Optional[str] = None
        self._move_receipt = None
        self._move_completed_at: Optional[float] = None
        self._move_started_at = 0.0
        self._move_started_wall = 0.0
        self._frame_floor = 0.0
        self._motor_position: Optional[int] = None
        self._tracker_generation: Optional[str] = None
        self._channel_center = None
        # One bounded channel-local association window also survives a candidate
        # ID disappearing, so a fresh tracker ID can be established without
        # briefly opening the feeder gate or retaining the rejected owner.
        self._continuity_confirmation_active = False
        self._continuity_confirmation_remaining = 0
        self._continuity_confirmation_last_ts = 0.0
        self._last_motion_observation_ts = 0.0
        self._resume_pending = False
        self._pause_motion_bounds = None
        self._resume_started_at = None
        self._resume_position_checked = False
        # Stall-watchdog progress signal (read by ClassificationChannelStateMachine):
        # last time the flow demonstrably moved forward — a real piece leaving
        # the channel, substantial piece movement, or a capture/classify/place
        # milestone. Deliberately NOT credited: phase changes (timeout ping-pong),
        # new track ids appearing, churn tracks retiring, and motor moves whose
        # piece never moved — those are exactly the wedges the watchdog must catch.
        self.last_progress_at = time.monotonic()
        # Multi-feed debounce: consecutive distinct frames showing >1 drop-zone id.
        self._multi_drop_streak = 0
        self._multi_drop_last_ts = -1.0
        # Monotonic counter for multi_drop_group ids; advanced once per new clump.
        self._multi_drop_seq = 0
        self.ctx.reset()
        self.ctx.known_object = None

    # ------------------------------------------------------------- watchdog API

    def noteProgress(self) -> None:
        self.last_progress_at = time.monotonic()

    def phaseName(self) -> str:
        return self._phase.value

    def attemptStallAutoClear(self, *, max_output_deg: float) -> ChannelClearResult:
        """Route uncertain retained C4 material through the installed reject drain."""
        self._hold_for_reject("C4 stall recovery found uncertain retained ownership")
        return ChannelClearResult(False, bool(self._pieces), 0.0, "reject_drain_requested")

    def automaticRejectReady(self) -> bool:
        """The main loop may dispatch the supported drain only after C4 stopped."""
        ready = (
            self._automatic_reject_requested
            and self._hold_stop_verified
            and not self._hold_stop_failed
        )
        if not ready:
            return False
        motor = getattr(getattr(self, "irl", None), "carousel_stepper", None)
        try:
            return not bool(getattr(motor, "stalled", False))
        except Exception:
            return False

    def automaticRejectStarted(self) -> None:
        self._automatic_reject_requested = False
        stats = getattr(self.gc, "runtime_stats", None)
        clear_request = getattr(stats, "clearC4RejectRequest", None)
        if callable(clear_request):
            clear_request()

    def _hold_for_reject(self, reason: str) -> None:
        """Latch a normal stop, then let the main loop use Complete C4 Recovery."""
        self._automatic_reject_requested = True
        stats = getattr(self.gc, "runtime_stats", None)
        request = getattr(stats, "requestC4Reject", None)
        if callable(request):
            request(reason)
        self._hold(reason)

    def hasSafetyHold(self) -> bool:
        return self._safety_hold_reason is not None

    def reconcilingPause(self) -> bool:
        return self._resume_pending and not self.hasSafetyHold()

    def restoreOccupiedCheckpoint(self, plan, state, frame_size):
        from .occupied_checkpoint import decode_owner, match_owners, RecoveryError
        matches = match_owners(plan, state, frame_size, time.time())
        if self._pieces or self.transport.activePieces() or self.transport.getPieceForDistributionDrop() is not None:
            raise RecoveryError("Recovery requires a new, empty controller")
        motor = self.irl.carousel_stepper
        if not motor.stationary_verified():
            raise RecoveryError("C4 is not stationary")
        position = motor.position
        owners = []
        for entry, observation in matches:
            obj = decode_owner(entry["object"])
            # No old movement or distribution readiness survives restart.
            obj.stage = PieceStage.created
            obj.dead = False
            obj.aborted = False
            tp = self._createPiece(observation.sv_bt_track_id, time.monotonic())
            tp.worker.ctx.known_object = obj
            tp.capture_done = True
            tp.result_applied = True
            tp.zone = observation.zone_code
            tp.bbox = observation.bbox
            tp.gap_to_exit = observation.com_forward_to_exit_deg
            tp.clearance_to_exit = observation.clearance_to_exit_deg
            tp.motor_position = position
            owners.append(obj)
        self._tracker_generation = state.tracker_generation
        self._channel_center = state.channel_center
        self._motor_position = position
        self._last_observation_ts = state.ts
        self._frame_floor = state.ts
        self._syncRetainedPieces()
        for tp in self._pieces.values():
            tp.worker.emitKnownObject()
        return owners

    def _syncRetainedPieces(self) -> None:
        stats = getattr(self.gc, "runtime_stats", None)
        if stats is None:
            return
        pieces = [tp.known_object for tp in self._pieces.values()]
        pieces.extend((self.transport.getPieceForDistributionPositioning(),
                       self.transport.getPieceForDistributionDrop()))
        stats.setRetainedC4Pieces(frozenset(
            obj.uuid for obj in pieces
            if obj is not None and obj.stage != PieceStage.distributed
        ))

    def _hold(self, reason: str) -> None:
        self._syncRetainedPieces()
        if self._safety_hold_reason is None:
            self._safety_hold_reason = reason
            self.logger.error(f"{LOG_TAG} holding retained pieces: {reason}")
        self.setClassificationReady(False, self._safety_hold_reason)
        self.serviceSafetyHold()

    def serviceSafetyHold(self) -> None:
        """Only stop and observe while latched; never run normal flow work."""
        if not self.hasSafetyHold():
            return
        self.setClassificationReady(False, self._safety_hold_reason)
        self.shared.set_distribution_gate(False, reason="c4_ownership_hold")
        now = time.monotonic()
        if self._hold_stop_started is None:
            self._hold_stop_started = now
        motor = self.irl.carousel_stepper
        try:
            self._hold_stop_verified = motor.stationary_verified()
            self._hold_stop_error = None
        except Exception as exc:
            self._hold_stop_verified = False
            self._hold_stop_error = str(exc)
        if not self._hold_stop_verified:
            if now - self._hold_stop_started >= 6.0:
                self._hold_stop_failed = True
            elif self._hold_stop_attempts < 3 and now - self._hold_stop_last_attempt >= 0.5:
                self._hold_stop_attempts += 1
                self._hold_stop_last_attempt = now
                try:
                    if not motor.move_at_speed(0, force=True):
                        self._hold_stop_error = "Safety stop was not acknowledged"
                except Exception as exc:
                    self._hold_stop_error = str(exc)
                # An ACK initiates braking. Only a later raw MCU observation
                # can verify the stop, including when software-disabled.
        self._publishSafetyHold()

    def _publishSafetyHold(self) -> None:
        stats = getattr(self.gc, "runtime_stats", None)
        active = stats.activeIncident() if stats is not None else None
        if stats is not None and (active is None or active.get("source_kind") == "two_piece_ownership"):
            # This is an ownership interlock, not an optional classification
            # warning. Clearing the popup must not clear the physical hold.
            payload = {
                "kind": CLASSIFICATION_TRACK_LOST_INCIDENT_KIND,
                "source_kind": "two_piece_ownership",
                "severity": "critical",
                "status": (
                    "waiting_for_operator" if self._hold_stop_failed
                    else "reject_pending" if self._automatic_reject_requested
                    else "waiting_for_operator"
                ),
                "awaiting_operator": self._hold_stop_failed or not self._automatic_reject_requested,
                "scope": "classification",
                "channel": "c4", "channel_label": "C4",
                "triggered_at": time.time(), "reason": self._safety_hold_reason,
                "track_ids": list(self._pieces),
                "stop_verified": self._hold_stop_verified,
                "stop_failed": self._hold_stop_failed,
                "stop_attempts": self._hold_stop_attempts,
                "stop_error": self._hold_stop_error,
                "operator_message": (
                    self._safety_hold_reason
                    + ". C4 stop could not be verified; a physical stop fault remains."
                    if self._hold_stop_failed else
                    self._safety_hold_reason
                    + ". C4 material will be routed to Reject by the supported full-pocket recovery."
                    if self._automatic_reject_requested else
                    self._safety_hold_reason
                    + ". A physical or motion-control condition requires attention."
                ),
            }
            if active is not None:
                payload["triggered_at"] = active["triggered_at"]
            if active is None or any(active.get(key) != value for key, value in payload.items()):
                stats.setActiveIncident(payload)

    # ------------------------------------------------------------------ main

    def step(self) -> None:
        self._syncRetainedPieces()
        if self.hasSafetyHold():
            self._hold(self._safety_hold_reason)
            return
        perception_service = getattr(self.gc, "perception_service", None)
        if perception_service is None:
            self._hold_for_reject("C4 perception is unavailable; retained material is uncertain")
            return
        state = perception_service.read_state(4)
        now = time.monotonic()
        frame_ts = float(state.ts)
        if not math.isfinite(frame_ts) or not 0 <= time.time() - frame_ts <= _MAX_FRAME_AGE_S:
            self._hold_for_reject("C4 perception is stale or has an invalid timestamp")
            return
        if self._resume_pending:
            self._reconcilePause(state, now)
            return
        if not self._pollMotion(now):
            if not self.hasSafetyHold():
                self._observeMovingExit(state)
            self.setClassificationReady(False, "awaiting motion completion")
            return
        if frame_ts <= self._frame_floor:
            # A delayed image can still witness this command's exit, but cannot
            # supply geometry for a new command before the planning fence.
            self._observeMovingExit(state)
            self.setClassificationReady(False, "awaiting post-completion observation")
            return
        stopped = True
        self._applyResults(now)
        if frame_ts <= self._last_observation_ts:
            return
        self._last_observation_ts = frame_ts
        self._observe(state, now)
        if self.hasSafetyHold():
            return
        missing = [tp for tp in self._pieces.values() if not tp.visible]
        # Only the actively commanded, exit-observed target can enter absence
        # confirmation. An unseen follower or a pre-exit loss blocks everything.
        uncertain = [tp for tp in missing if not (
            tp is self._eject_target and tp.exit_seen_ts is not None
            and self._eject_move_accepted
        )]
        if uncertain:
            self._resetExitConfirmation()
            self.setClassificationReady(False, "C4 owner temporarily unseen")
            self.stopStepper()
            if any(now - tp.last_seen > _EJECT_TIMEOUT_S for tp in uncertain):
                self._hold_for_reject("C4 retained piece could not be reacquired")
            return
        if any(po.sv_bt_track_id is None for po in state.pieces):
            self._resetExitConfirmation()
            self._hold_for_reject("C4 has an unidentified observation")
            return

        if self._continuity_confirmation_active or self._hasUnconfirmedContinuity():
            self.setClassificationReady(False, "awaiting fresh C4 track continuity")
            return

        # The classification channel OWNS the feeder admission gate. Ready only
        # when we are idle between cycles (not mid-rotation) AND the drop zone is
        # clear AND the platter has settled — i.e. "rotation complete, drop empty".
        ready = self._phase == _Phase.WAITING and (not state.in_drop) and stopped
        self.setClassificationReady(ready, "waiting + drop clear + stopped")

        if self._phase == _Phase.WAITING:
            if stopped:
                self._captureDropPieces(perception_service, now)
                self._aimChuteForHead(now)
                self._maybeStartRotation()
        elif self._phase == _Phase.EJECTING:
            self._ejecting(state, stopped, now)
        elif self._phase == _Phase.STAGING:
            self._staging(state, stopped, now)

    # ------------------------------------------------- perception reconciliation

    def _observeMovingExit(self, state) -> None:
        """Observe a release in flight without using moving geometry to advance.

        The target may fall before the first post-stop image. Its ID, tracker
        lifetime and observed angle must still fit this specific command.
        """
        if state.ts < self._move_started_wall or state.ts <= max(
            self._last_observation_ts, self._last_motion_observation_ts
        ):
            return
        self._last_motion_observation_ts = state.ts
        if state.tracker_generation != self._tracker_generation:
            self._hold_for_reject("C4 tracker changed during commanded motion")
            return
        if state.channel_center != self._channel_center:
            self._hold_for_reject("C4 rotation geometry changed during commanded motion")
            return
        target = self._eject_target
        for po in state.pieces:
            if po.zone_code == 2 and (target is None or po.sv_bt_track_id != target.track_id):
                self._hold_for_reject("C4 non-target piece entered the exit during motion")
                return
        if target is None or self._move_receipt is None:
            return
        observations = [po for po in state.pieces if po.sv_bt_track_id == target.track_id]
        if len(observations) > 1:
            self._hold_for_reject("C4 moving target has duplicate identities")
            return
        if not observations:
            return
        po = observations[0]
        if not self._bboxContinuous(target.bbox, po.bbox):
            self._hold_for_reject("C4 moving target has implausible spatial continuity")
            return
        if target.gap_to_exit is None or not math.isfinite(po.com_forward_to_exit_deg):
            self._hold_for_reject("C4 moving target geometry is unavailable")
            return
        receipt = self._move_receipt
        steps = ((receipt.target_position - receipt.start_position + 2**31) % 2**32) - 2**31
        platter = C4FiveSectorPlatter.from_irl_config(self.irl_config)
        travel = platter.motor_microsteps_to_output_degrees(steps) * C4_TRAVEL_SIGN
        observed = (target.gap_to_exit - po.com_forward_to_exit_deg + _PROGRESS_MOVE_DEG) % 360 - _PROGRESS_MOVE_DEG
        if not -_PROGRESS_MOVE_DEG <= observed <= travel + _PROGRESS_MOVE_DEG:
            self._hold("C4 moving target is outside its commanded travel")
            return
        target.exit_seen_ts = state.ts if po.zone_code == 2 else None

    def _observe(self, state, now: float, *, allow_arrivals: bool = False) -> None:
        """Match this frame's observations to tracked pieces by track id, create
        pieces for new ids, retire pieces whose id has been gone too long, and
        flag double feeds."""
        generation = state.tracker_generation
        if not generation:
            self._hold_for_reject("C4 tracker generation is unavailable")
            return
        frame_ts = float(getattr(state, "ts", 0.0))
        if (self._continuity_confirmation_active and math.isfinite(frame_ts)
                and frame_ts > self._continuity_confirmation_last_ts):
            self._continuity_confirmation_remaining -= 1
            self._continuity_confirmation_last_ts = frame_ts
        if self._tracker_generation is not None and generation != self._tracker_generation:
            provisional_only = bool(self._pieces) and all(
                not tp.continuity_confirmed and tp.known_object is None
                and not tp.capture_done and tp is not self._eject_target
                and tp is not self._stage_target
                for tp in self._pieces.values()
            )
            if self._pieces and not provisional_only:
                self._hold_for_reject("C4 tracker reset with retained owners")
                return
            if provisional_only:
                self._hold_for_reject("C4 tracker reset with uncertain provisional pieces")
                return
            self._completed_ids.clear()
        self._tracker_generation = generation
        center = state.channel_center
        if center is None or len(center) != 2 or not all(math.isfinite(v) for v in center):
            self._hold_for_reject("C4 rotation center is unavailable")
            return
        if self._channel_center is not None and center != self._channel_center and self._pieces:
            self._hold_for_reject("C4 rotation geometry changed with retained owners")
            return
        self._channel_center = center
        if state.n_pieces != len(state.pieces):
            self._hold_for_reject("C4 occupancy and piece observations disagree")
            return
        ids = [po.sv_bt_track_id for po in state.pieces if po.sv_bt_track_id is not None]
        has_unidentified_piece = any(po.sv_bt_track_id is None for po in state.pieces)
        if has_unidentified_piece:
            self._hold_for_reject("C4 has an unidentified observation")
            return
        if len(ids) != len(set(ids)):
            self._hold_for_reject("C4 frame contains duplicate physical identities")
            return
        if any(not math.isfinite(po.com_forward_to_exit_deg)
               or not all(math.isfinite(value) for value in po.bbox)
               or po.bbox[2] <= po.bbox[0] or po.bbox[3] <= po.bbox[1]
               for po in state.pieces):
            self._hold_for_reject("C4 piece geometry is invalid")
            return
        for tp in self._pieces.values():
            tp.visible = False
        newly_confirmed = False
        for po in getattr(state, "pieces", ()):
            tid = po.sv_bt_track_id
            if tid is None:
                continue  # untracked box — counts for zone occupancy, not identity
            if tid in self._completed_ids:
                self._hold_for_reject(f"C4 discharged track {tid} reappeared")
                return
            tp = self._pieces.get(tid)
            if tp is not None and not tp.continuity_confirmed:
                if (not math.isfinite(frame_ts)
                        or (tp.continuity_last_ts is not None
                            and frame_ts <= tp.continuity_last_ts)):
                    # Duplicate/old images cannot mutate even a provisional zone
                    # or add association evidence.
                    tp.visible = True
                    continue
            if (tp is not None and not tp.continuity_confirmed
                    and int(po.zone_code) != tp.continuity_zone):
                self._hold_for_reject(
                    f"C4 provisional track {tid} changed zones before continuity was confirmed"
                )
                return
            created = tp is None
            if tp is None:
                if self._phase != _Phase.WAITING and not (
                    allow_arrivals and po.zone_code == _ZONE_DROP
                ):
                    self._hold_for_reject(f"C4 new identity {tid} appeared during rotation")
                    return
                tp = self._createPiece(tid, now)
                if (int(po.zone_code) == _ZONE_DROP
                        or self._continuity_confirmation_active):
                    tp.continuity_confirmed = False
                    # An unidentified co-observation makes this initial
                    # association ambiguous too; the next clean pair must not
                    # mature from one comparison alone.
                    tp.continuity_suspect = has_unidentified_piece
                    tp.continuity_generation = generation
                    tp.continuity_zone = int(po.zone_code)
                    tp.continuity_last_ts = frame_ts
                    self._startContinuityConfirmation(frame_ts)

            failure = None
            if not created:
                if not tp.continuity_confirmed:
                    if tp.continuity_generation != generation:
                        failure = "C4 tracker generation changed during new-track confirmation"
                    elif has_unidentified_piece:
                        failure = "C4 new-track frame contains an unidentified observation"
                    elif int(po.zone_code) != tp.continuity_zone:
                        failure = "C4 new track changed zones before continuity confirmation"
                    elif tp.motor_position is None or tp.gap_to_exit is None or self._motor_position is None:
                        failure = "C4 new-track continuity baseline is unavailable"
                    elif int(self._motor_position) != int(tp.motor_position):
                        self._hold("C4 rotor moved while new-track continuity was unconfirmed")
                        return
                    else:
                        failure = self._continuityFailure(tp, po)
                elif tp.motor_position is not None and tp.gap_to_exit is not None:
                    failure = self._continuityFailure(tp, po)

            was_confirmed = tp.continuity_confirmed
            if failure is not None:
                # A failed identity/position comparison remains reject-bound,
                # even if this was still a provisional tracker association.
                self._hold_for_reject(failure)
                return
            elif not created and not was_confirmed:
                if tp.continuity_suspect:
                    tp.continuity_stable_pairs += 1
                    if tp.continuity_stable_pairs >= _NEW_TRACK_STABLE_PAIRS:
                        tp.continuity_confirmed = True
                        newly_confirmed = True
                else:
                    # Normal new tracks pass on their first eligible comparison.
                    tp.continuity_confirmed = True
                    newly_confirmed = True

            tp.zone = int(po.zone_code)
            b = po.bbox
            tp.bbox = (int(b[0]), int(b[1]), int(b[2]), int(b[3]))
            tp.gap_to_exit = po.com_forward_to_exit_deg
            tp.clearance_to_exit = po.clearance_to_exit_deg
            tp.motor_position = self._motor_position
            tp.last_seen = now
            tp.visible = True
            tp.absent_since_ts = None
            tp.absent_frames = 0
            tp.settled_absent_since = None
            tp.settled_absent_frames = 0
            if not tp.continuity_confirmed:
                tp.continuity_last_ts = frame_ts
            if tp is self._eject_target:
                tp.exit_seen_ts = float(state.ts) if tp.zone == 2 else None
            # Watchdog progress: the piece physically moved a substantial amount.
            if tp.gap_to_exit is not None and was_confirmed:
                gap = float(tp.gap_to_exit)
                if tp.progress_gap is None:
                    tp.progress_gap = gap
                elif abs(gap - tp.progress_gap) > _PROGRESS_MOVE_DEG:
                    tp.progress_gap = gap
                    self.noteProgress()

        for tp in self._pieces.values():
            if not tp.visible:
                if tp.absent_since_ts is None:
                    tp.absent_since_ts = float(state.ts)
                tp.absent_frames += 1
        rejected = [
            tid for tid, tp in self._pieces.items()
            if not tp.visible and not tp.continuity_confirmed and tp.known_object is None
        ]
        for tid in rejected:
            self._hold_for_reject(
                f"C4 provisional track {tid} disappeared before continuity was confirmed"
            )
            return
        if newly_confirmed and not self._hasUnconfirmedContinuity():
            self._continuity_confirmation_active = False
            self._continuity_confirmation_remaining = 0
        if (self._continuity_confirmation_active
                and self._continuity_confirmation_remaining <= 0):
            self._hold_for_reject(
                "C4 track association could not be re-established within fresh observations"
            )
            return
        if self._retireGonePieces(now):
            return
        self._flagDoubleFeeds(state)

    def _hasUnconfirmedContinuity(self) -> bool:
        return any(not tp.continuity_confirmed for tp in self._pieces.values())

    def _startContinuityConfirmation(self, frame_ts: float) -> None:
        if self._continuity_confirmation_active:
            return
        self._continuity_confirmation_active = True
        self._continuity_confirmation_remaining = _NEW_TRACK_CONFIRMATION_FRAMES
        self._continuity_confirmation_last_ts = frame_ts

    def _continuityFailure(self, tp: _TrackedPiece, po) -> Optional[str]:
        if self._motor_position is None or tp.motor_position is None or tp.gap_to_exit is None:
            return f"C4 track {tp.track_id} continuity baseline is unavailable"
        if not self._bboxContinuous(tp.bbox, po.bbox):
            return f"C4 track {tp.track_id} has implausible spatial continuity"
        platter = C4FiveSectorPlatter.from_irl_config(self.irl_config)
        steps = ((self._motor_position - tp.motor_position + 2**31) % 2**32) - 2**31
        travel = platter.motor_microsteps_to_output_degrees(steps) * C4_TRAVEL_SIGN
        expected = tp.gap_to_exit - travel
        residual = (float(po.com_forward_to_exit_deg) - expected + 180) % 360 - 180
        if not math.isfinite(residual) or abs(residual) > _PROGRESS_MOVE_DEG:
            return f"C4 track {tp.track_id} has implausible motion continuity"
        if steps == 0:
            old_x, old_y = (tp.bbox[0] + tp.bbox[2]) / 2, (tp.bbox[1] + tp.bbox[3]) / 2
            new_x, new_y = (po.bbox[0] + po.bbox[2]) / 2, (po.bbox[1] + po.bbox[3]) / 2
            tolerance = max(5.0, min(tp.bbox[2] - tp.bbox[0], tp.bbox[3] - tp.bbox[1]) / 4)
            if math.hypot(new_x - old_x, new_y - old_y) > tolerance:
                return f"C4 track {tp.track_id} moved spatially while the rotor was stationary"
        return None

    def _bboxContinuous(self, previous, current) -> bool:
        if self._channel_center is None or not all(math.isfinite(v) for v in current):
            return False
        cx, cy = self._channel_center
        def shape(box):
            x1, y1, x2, y2 = box
            if x2 <= x1 or y2 <= y1:
                return None
            return (math.hypot((x1 + x2) / 2 - cx, (y1 + y2) / 2 - cy),
                    math.hypot(x2 - x1, y2 - y1))
        old, new = shape(previous), shape(current)
        if old is None or new is None:
            return False
        # Rotation preserves radius. The AABB diagonal can vary with orientation,
        # but a gross size change or radial jump is not the same physical owner.
        return abs(new[0] - old[0]) <= max(5.0, old[0] * 0.1) and 0.5 <= new[1] / old[1] <= 2.0

    def _createPiece(self, track_id: int, now: float) -> _TrackedPiece:
        # Track the id for position immediately, but DEFER the KnownObject: it's
        # created only when the piece is actually photographed (a real drop
        # arrival) or routed to distribution (see _ensureKnownObject). A transient
        # detection that appears mid-channel and does neither is tracked but never
        # becomes a UI 'piece' — no spam of pending objects, no false multi-drop.
        worker = Rev01BaseState(*self._deps, SimpleStateMachineRev01Context())
        worker.ctx.reset()
        worker.ctx.known_object = None
        tp = _TrackedPiece(track_id, worker, now)
        self._pieces[track_id] = tp
        return tp

    def _ensureKnownObject(self, tp: _TrackedPiece) -> None:
        if tp.worker.ctx.known_object is not None:
            return
        tp.worker.ctx.known_object = KnownObject(
            stage=PieceStage.created,
            classification_status=ClassificationStatus.pending,
            first_carousel_seen_ts=time.time(),
        )
        self._syncRetainedPieces()
        tp.worker.emitKnownObject()
        self.logger.info(f"{LOG_TAG} new piece track={tp.track_id}")

    def _retireGonePieces(self, now: float) -> bool:
        gone = [
            tid
            for tid, tp in self._pieces.items()
            if (now - tp.last_seen) > _TRACK_GONE_RETIRE_S
            and tp.known_object is None and not tp.capture_done
            and tp is not self._eject_target and tp is not self._stage_target
        ]
        if gone:
            self._hold_for_reject(
                f"C4 unowned track {gone[0]} disappeared before its physical disposition was known"
            )
            return True
        return False

    def _flagDoubleFeeds(self, state) -> None:
        drop = [tp for tp in self._pieces.values()
                if tp.visible and tp.zone == _ZONE_DROP and tp.continuity_confirmed]
        frame_ts = float(getattr(state, "ts", 0.0))
        if frame_ts != self._multi_drop_last_ts:
            self._multi_drop_last_ts = frame_ts
            self._multi_drop_streak = self._multi_drop_streak + 1 if len(drop) > 1 else 0
        threshold = max(1, int(self.ctx.config.multi_feed_confirm_reads))
        if self._multi_drop_streak < threshold:
            return
        # Bind this whole clump under one multi_drop_group id. Reuse an existing
        # group already present in the drop zone so a third piece joining a known
        # clump is tied to the same logical multi-drop rather than starting a new one.
        group = next(
            (tp.multi_drop_group for tp in drop if tp.multi_drop_group is not None),
            None,
        )
        if group is None:
            self._multi_drop_seq += 1
            group = self._multi_drop_seq
        for tp in drop:
            # A later arrival must not invalidate an already isolated capture
            # or its result. Only the new, uncaptured members are uncertain.
            if not tp.double_feed and not tp.capture_done:
                self._markDoubleFeed(tp, group)

    def _markDoubleFeed(self, tp: _TrackedPiece, group: int) -> None:
        # Two+ pieces in the drop zone at once: classification can't be trusted, so
        # skip it and route the piece to the misc bin. It still rides the normal
        # bucket-brigade (staged, then ejected) — just to misc. All members of the
        # clump share one multi_drop_group so they read as a single multi-drop.
        tp.double_feed = True
        tp.multi_drop_group = group
        tp.capture_done = True
        tp.result_applied = True
        self.noteProgress()
        self._ensureKnownObject(tp)
        obj = tp.known_object
        if obj is not None:
            obj.classification_status = ClassificationStatus.multi_drop_fail
            obj.part_id = None
            obj.destination_bin = None
            obj.moving_avg_price = None
            obj.high_value_routed = False
            obj.not_in_inventory = None
            tp.worker.emitKnownObject()
        self.logger.warning(
            f"{LOG_TAG} double feed -> misc (track={tp.track_id}, group={group})"
        )

    # --------------------------------------------------- ordered-queue accessors

    def _headPiece(self) -> Optional[_TrackedPiece]:
        # The head of the queue: the most-forward piece that has LEFT the drop zone
        # (in precise, the exit approach, or the unnamed gap between drop and
        # precise). This is the piece we classify-aim and eject next. Including the
        # gap (zone NONE) is what keeps a clump piece that overshot precise, or a
        # stray that landed mid-channel, from being stranded.
        fwd = [tp for tp in self._pieces.values() if tp.zone != _ZONE_DROP]
        if not fwd:
            return None
        return min(fwd, key=lambda tp: tp.gap_to_exit if tp.gap_to_exit is not None else 1e9)

    def _dropPiece(self) -> Optional[_TrackedPiece]:
        drop = [tp for tp in self._pieces.values() if tp.zone == _ZONE_DROP]
        if not drop:
            return None
        return min(drop, key=lambda tp: tp.gap_to_exit if tp.gap_to_exit is not None else 1e9)

    # -------------------------------------------- capture / classify / aim chute

    def _captureDropPieces(self, perception_service, now: float) -> None:
        # Photograph pieces ONLY while they sit at rest in the drop zone (the
        # user's rule). Each piece is cropped from its OWN tracked bbox, so two
        # pieces never cross-contaminate each other's burst.
        raw = perception_service.read_pieces_and_frame(4)
        if raw is None:
            return
        observations, perc_frame = raw
        # Never use a bbox from one inference result on a newer camera frame.
        if float(perc_frame.timestamp) != self._last_observation_ts:
            return
        boxes = {po.sv_bt_track_id: po.bbox for po in observations}
        for tp in list(self._pieces.values()):
            if (not tp.visible or tp.zone != _ZONE_DROP or tp.capture_done
                    or tp.double_feed or not tp.continuity_confirmed):
                continue
            if tp.track_id not in boxes:
                continue
            self._ensureKnownObject(tp)  # real drop arrival -> becomes a UI piece
            ctx = tp.worker.ctx
            if ctx.capturing_started_at == 0.0:
                ctx.capturing_started_at = now
            self._cropBurstFrame(tp.worker, boxes[tp.track_id], perc_frame)
            done, reason = tp.worker.burstCaptureComplete(ctx, now)
            if done:
                tp.capture_done = True
                self.noteProgress()
                ctx.classify_started_at = now
                caps = list(ctx.captured_crops)
                if caps:
                    tp.worker.spawnClassifyThread(caps)
                else:
                    ctx.classification_error = "no_captures"
                self.logger.info(
                    f"{LOG_TAG} captured track={tp.track_id} "
                    f"({len(caps)} crops, stop={reason}); classifying"
                )

    def _applyResults(self, now: float) -> None:
        for tp in self._pieces.values():
            if tp.result_applied or tp.double_feed:
                continue
            ctx = tp.worker.ctx
            if ctx.classify_started_at == 0.0:
                continue
            with ctx.classify_lock:
                result = ctx.classification_result
                error = ctx.classification_error
            if result is not None or error is not None:
                tp.worker.updateKnownObjectWithResult(result, error)
                tp.result_applied = True
                self.noteProgress()
            elif (now - ctx.classify_started_at) > ctx.config.classify_timeout_s:
                self.logger.error(f"{LOG_TAG} classify timeout track={tp.track_id} -> unknown")
                tp.worker.updateKnownObjectWithResult(None, "timeout")
                tp.result_applied = True
                self.noteProgress()

    def _aimChuteForHead(self, now: float) -> None:
        # Hand the head to distribution so the chute aims while the next piece is
        # captured (the throughput overlap). Distribution has a single slot, so
        # only ever place the head; the next piece is placed after this one ejects
        # and frees the slot.
        tp = self._headPiece()
        if tp is None or tp.placed:
            return
        if not tp.result_applied:
            # A forward piece that was never even captured (stray / churn leftover /
            # a multi-drop sibling that skipped the drop zone) would otherwise sit
            # as an un-shippable head forever. After a grace period, send it to misc
            # so it can be ejected and the queue drains.
            if tp.worker.ctx.classify_started_at == 0.0 and (now - tp.created_at) > _STRAY_MISC_S:
                self._ensureKnownObject(tp)
                obj0 = tp.known_object
                if obj0 is not None:
                    obj0.classification_status = ClassificationStatus.unknown
                    obj0.part_id = None
                    tp.worker.emitKnownObject()
                tp.result_applied = True
                self.logger.warning(
                    f"{LOG_TAG} stray head track={tp.track_id} never classified -> misc"
                )
            else:
                return
        self._ensureKnownObject(tp)
        obj = tp.known_object
        if obj is None:
            return
        if obj.part_id is None and obj.classification_status in (
            ClassificationStatus.pending,
            ClassificationStatus.classifying,
        ):
            obj.classification_status = ClassificationStatus.unknown
        self.transport.placePieceForDistribution(obj)
        tp.placed = True
        self.noteProgress()
        self.logger.info(
            f"{LOG_TAG} aiming chute for head track={tp.track_id} "
            f"(status={obj.classification_status})"
        )

    def _headReady(self, tp: _TrackedPiece) -> bool:
        # Classified AND the chute is physically aimed for this piece.
        obj = tp.known_object
        positioned = bool(
            tp.placed
            and self.shared.distribution_ready
            and obj is not None
            and obj.stage == PieceStage.distributing
            and self.transport.getPieceForDistributionPositioning() is obj
        )
        if not positioned:
            return False
        try:
            target_layer = obj.destination_bin[0] if obj.destination_bin is not None else None
            return flap_path_settled(self.irl.servos, target_layer)
        except Exception as exc:
            self._hold(f"C4 distribution flap failure: {exc}")
            return False

    # ----------------------------------------------------------------- movement

    def _pollMotion(self, now: float) -> bool:
        stepper = self.irl.carousel_stepper
        try:
            if stepper.software_disabled or not stepper.enabled or stepper.stalled:
                raise RuntimeError("C4 motor disabled or stalled")
            if self._move_receipt is not None:
                if not stepper.tracked_move_complete(self._move_receipt):
                    if now - self._move_started_at > _EJECT_TIMEOUT_S:
                        raise RuntimeError("C4 motion completion timed out")
                    return False
                if self._move_completed_at is None:
                    self._move_completed_at = time.time()
                    self._frame_floor = self._move_completed_at
                self._motor_position = self._move_receipt.target_position
            else:
                if not stepper.stopped:
                    raise RuntimeError("C4 has motion without an owned command")
                position = int(stepper.position)
                if self._motor_position is not None and position != self._motor_position:
                    raise RuntimeError("C4 position changed without an owned command")
                self._motor_position = position
            return True
        except Exception as exc:
            self._hold(f"C4 motion failure: {exc}")
            return False

    def _startTrackedOutputMove(self, degrees: float, speed: int) -> bool:
        try:
            platter = C4FiveSectorPlatter.from_irl_config(self.irl_config)
            steps = platter.output_degrees_to_motor_microsteps(degrees)
            # Rounding must not exceed an edge-clearance limit.
            if abs(platter.motor_microsteps_to_output_degrees(steps)) > abs(degrees):
                steps -= 1 if steps > 0 else -1
            self._move_receipt = self.irl.carousel_stepper.start_tracked_move(steps, speed)
            self._move_completed_at = None
            self._move_started_at = time.monotonic()
            self._move_started_wall = time.time()
            self._last_motion_observation_ts = 0.0
            self._resetExitConfirmation()
            return True
        except Exception as exc:
            self._hold(f"C4 move failed: {exc}")
            return False

    def _resetExitConfirmation(self) -> None:
        if self._eject_target is not None:
            self._eject_target.settled_absent_since = None
            self._eject_target.settled_absent_frames = 0

    def _maybeStartRotation(self) -> None:
        # A ready head can leave independently of a new arrival. In particular,
        # the final rejected follower must not wait forever for another piece.
        #   1. no head on the channel       -> stage the drop piece (no eject)
        #   2. head ready to ship           -> eject it AND stage the drop piece
        # If a head exists but is not ready yet, we wait (don't rotate a not-ready
        # piece toward the fall-off).
        drop = self._dropPiece()
        if any(tp.zone == _ZONE_DROP and not tp.capture_done for tp in self._pieces.values()):
            return
        head = self._headPiece()
        if head is None and drop is not None:
            self._stage_target = drop
            self._eject_target = None
            self._enterPhase(_Phase.STAGING)
            self.logger.info(f"{LOG_TAG} ROTATE: stage track={drop.track_id} (no head)")
        elif head is not None and self._headReady(head):
            self._stage_target = drop
            self._eject_target = head
            self._eject_move_accepted = False
            head.exit_seen_ts = None
            self._enterPhase(_Phase.EJECTING)
            self.logger.info(
                f"{LOG_TAG} ROTATE: eject track={head.track_id}"
            )

    def _ejecting(self, state, stopped: bool, now: float) -> None:
        target = self._eject_target
        if target is None:
            self._enterPhase(_Phase.STAGING)
            return
        timed_out = (now - self._phase_started_at) > _EJECT_TIMEOUT_S
        if timed_out:
            self._hold_for_reject(f"C4 exit confirmation timed out for track {target.track_id}")
            return
        if not self._headReady(target):
            self._hold_for_reject(f"C4 distribution route lost for track {target.track_id}")
            return
        if not target.visible:
            if any(po.zone_code == 2 for po in state.pieces):
                self._hold_for_reject("C4 exit remains occupied while its target is unseen")
                return
            if not stopped:
                self._resetExitConfirmation()
                return
            # Start evidence at observed motor completion, not the first absent
            # frame during rotation. Delayed pre-stop frames cannot qualify.
            if target.settled_absent_since is None:
                target.settled_absent_since = time.time()
            if self._last_observation_ts >= target.settled_absent_since:
                target.settled_absent_frames += 1
            confirmed = (
                self._eject_move_accepted and target.exit_seen_ts is not None
                and self._move_receipt is not None and self._move_completed_at is not None
                and target.absent_since_ts is not None
                and target.absent_since_ts > target.exit_seen_ts
                and target.settled_absent_frames >= 2
                and self._last_observation_ts - target.settled_absent_since >= _EJECT_GONE_CONFIRM_S
            )
            if not confirmed:
                return
            self.transport.advanceTransport()
            self._completed_ids.add(target.track_id)
            self._pieces.pop(target.track_id)
            self.noteProgress()
            self.logger.info(f"{LOG_TAG} exit-confirmed track={target.track_id}")
            self._eject_target = None
            self._enterPhase(_Phase.STAGING)
            return
        if stopped:
            if not state.pieces or state.pieces[0].sv_bt_track_id != target.track_id:
                self._hold_for_reject("C4 exit target is no longer the observed leading piece")
                return
            gap = state.exit_com_forward_to_center_deg
            if gap is None or not math.isfinite(gap):
                self._hold_for_reject("C4 exit geometry is unavailable")
                return
            tolerance = self.ctx.config.discharge_center_tolerance_deg
            needs_release_command = target.zone == 2 and not self._eject_move_accepted
            if gap > tolerance or needs_release_command:
                # Pause invalidates both completed and interrupted receipts.
                # A target already at the center needs a new bounded release
                # command, not a zero-distance wait or inferred delivery. Use
                # at most the existing tolerance (one perception degree minimum)
                # and apply the same follower/max-travel limits as every eject.
                distance = gap if gap > tolerance else max(1.0, tolerance)
                move = min(self.ctx.config.discharge_max_move_output_deg, distance)
                followers = [tp for tp in self._pieces.values() if tp is not target]
                for follower in followers:
                    clearance = follower.clearance_to_exit
                    if not follower.visible or clearance is None or not math.isfinite(clearance) or clearance <= 0:
                        self._hold_for_reject("C4 follower has no verified exit clearance")
                        return
                    move = min(move, clearance)
                accepted = self._startTrackedOutputMove(
                    C4_TRAVEL_SIGN * move, self.ctx.config.discharge_speed_usteps_per_s
                )
                if not accepted:
                    self._hold("C4 discharge move was not acknowledged")
                    return
                self._eject_move_accepted = True

    def _staging(self, state, stopped: bool, now: float) -> None:
        # Advance the platter until the DROP ZONE IS CLEAR — i.e. the new piece and
        # any multi-drop siblings have all left the drop zone (into the holding
        # area). Keying on "drop clear" rather than "one chosen piece reached
        # precise" is what moves a clump through together instead of stranding the
        # trailing piece.
        if (now - self._phase_started_at) > _STAGE_TIMEOUT_S:
            self._hold_for_reject("C4 staging timed out with retained pieces")
            return
        if not stopped:
            return
        drop_clear = (not state.in_drop) and not any(
            tp.zone == _ZONE_DROP for tp in self._pieces.values()
        )
        if drop_clear:
            self.logger.info(f"{LOG_TAG} staged -> holding (drop clear)")
            self._stage_target = None
            self._enterPhase(_Phase.WAITING)
            return
        # Size the nudge by the leading piece's gap to the precise entry so the head
        # converges onto the holding band; once the leading piece is already at/past
        # precise (gap <= tol) but the drop zone still isn't clear, fall back to a
        # fixed step to keep pushing the clump out.
        gap = state.exit_com_forward_to_precise_deg
        if gap is None or not math.isfinite(gap):
            self._hold_for_reject("C4 staging geometry is unavailable")
            return
        move = _STAGE_STEP_DEG
        tol = self.ctx.config.precise_center_tolerance_deg
        if gap is not None:
            lead_to_exit = state.exit_com_forward_deg
            # comForwardToPreciseEntryDeg wraps to (-180, 180]; a piece that lands
            # far up the drop zone reads as a small/negative gap when it is really a
            # near-full turn short. Un-wrap when the leading gap-to-exit shows it is
            # clearly upstream.
            if gap <= tol and lead_to_exit is not None and lead_to_exit > 180.0:
                gap += 360.0
            if gap > tol:
                move = min(self.ctx.config.discharge_max_move_output_deg, gap)
        for tp in self._pieces.values():
            clearance = tp.clearance_to_exit
            if not tp.visible or clearance is None or not math.isfinite(clearance) or clearance <= 0:
                self._hold_for_reject("C4 staging has no verified clearance before the exit")
                return
            move = min(move, clearance)
        if not self._startTrackedOutputMove(
            C4_TRAVEL_SIGN * move, self.ctx.config.precise_converge_speed_usteps_per_s
        ):
            self._hold("C4 staging move was not acknowledged")

    def _enterPhase(self, phase: _Phase) -> None:
        # Deliberately NOT a watchdog progress credit: the eject/stage timeouts
        # re-enter phases every _*_TIMEOUT_S, so a wedged piece would ping-pong
        # STAGING <-> WAITING forever and never trip the stall incident. Real
        # progress is credited where pieces demonstrably move or complete a
        # milestone instead.
        self._phase = phase
        self._phase_started_at = time.monotonic()
        if phase != _Phase.WAITING:
            self.setClassificationReady(False, "C4 rotation owns intake")

    # ------------------------------------------------------------------ helpers

    def _cropBurstFrame(self, worker: Rev01BaseState, bbox, perc_frame) -> None:
        ctx = worker.ctx
        if perc_frame.bgr is None or bbox is None:
            return
        frame_ts = float(perc_frame.timestamp)
        if frame_ts <= ctx.last_capture_frame_ts:
            return
        crop = self.cv.cropBbox(perc_frame.bgr, bbox, ctx.config.crop_padding_px)
        if crop is None:
            return
        correct = getattr(perc_frame, "correct_pixels", None)
        if correct is not None:
            crop = correct(crop, "recognition_crop_color_ms")
        sharp = self.sharpness(crop)
        quality = crop_quality.scoreCrop(crop)
        ctx.captured_crops.append(crop)
        ctx.captured_crop_sharpness.append(sharp)
        ctx.captured_crop_quality.append(quality)
        ctx.captured_crop_timestamps.append(frame_ts)
        ctx.last_capture_frame_ts = frame_ts
        obj = ctx.known_object
        if obj is not None:
            encoded = self.encodeFrame(crop)
            if encoded is not None:
                obj.latest_captured_crop = encoded
                obj.latest_captured_crop_ts = frame_ts
                obj.recognition_image_set.append(
                    RecognitionImage(
                        image=encoded,
                        source="c4_burst",
                        used=False,
                        ts=frame_ts,
                        channel=4,
                        created_at=frame_ts,
                        sharpness=sharp,
                    )
                )
            worker.emitKnownObject()

    def cleanup(self) -> None:
        # Pause and stop both invoke cleanup in the deployed lifecycle. Neither
        # establishes physical clearance. Retain all owners (and their workers)
        # rather than turning a resumed piece into a new unknown object.
        self._syncRetainedPieces()
        self.setClassificationReady(False, "C4 pause reconciliation")
        if self.hasSafetyHold() or self._resume_pending:
            return
        try:
            motor = self.irl.carousel_stepper
            current = int(motor.position)
            start = self._motor_position if self._motor_position is not None else current
            end = self._move_receipt.target_position if self._move_receipt is not None else start
            self._pause_motion_bounds = (start, end)
            if not motor.move_at_speed(0, force=True):
                raise RuntimeError("pause stop not acknowledged")
            self._resume_pending = True
            self._resume_started_at = None
            self._resume_position_checked = False
            self._eject_move_accepted = False
            self._resetExitConfirmation()
            if self._eject_target is not None:
                self._eject_target.exit_seen_ts = None
        except Exception as exc:
            self._hold(f"C4 pause stop failure: {exc}")

    def _reconcilePause(self, state, now: float) -> None:
        """Resume retained owners only after stopped, fresh continuity checks.

        An interrupted eject is not a delivery. It needs a new commanded move
        and new exit evidence; a missing owner remains retained.
        """
        self.setClassificationReady(False, "C4 pause reconciliation")
        if self._resume_started_at is None:
            self._resume_started_at = now
        try:
            motor = self.irl.carousel_stepper
            if motor.software_disabled or not motor.enabled or motor.stalled:
                raise RuntimeError("motor unavailable after pause")
            if not motor.stationary_verified():
                if now - self._resume_started_at > 6:
                    raise RuntimeError("pause stop did not settle")
                return
            current = int(motor.position)
            start, end = self._pause_motion_bounds
            total = ((end - start + 2**31) % 2**32) - 2**31
            actual = ((current - start + 2**31) % 2**32) - 2**31
            if not min(0, total) <= actual <= max(0, total):
                raise RuntimeError("position outside the paused command's travel")
            if not self._resume_position_checked:
                self._move_receipt = None
                self._move_completed_at = None
                self._motor_position = current
                self._frame_floor = time.time()
                self._resume_position_checked = True
                return
            if current != self._motor_position:
                raise RuntimeError("unowned movement during pause reconciliation")
            if state.ts <= max(self._frame_floor, self._last_observation_ts):
                return
            self._last_observation_ts = state.ts
            self._observe(state, now, allow_arrivals=True)
            if self.hasSafetyHold():
                return
            if self._continuity_confirmation_active or self._hasUnconfirmedContinuity():
                return
            if any(not tp.visible for tp in self._pieces.values()) or any(
                po.sv_bt_track_id is None for po in state.pieces
            ):
                if now - self._resume_started_at > _EJECT_TIMEOUT_S:
                    self._hold_for_reject("retained owner cannot be located after pause")
                    return
                return
            self._resume_pending = False
            # Any late, now-observed intake is captured/rejected individually
            # before restarting the retained head's discharge.
            self._phase = _Phase.WAITING
            self._eject_target = None
            self._stage_target = None
            self._phase_started_at = now
            self.noteProgress()
        except Exception as exc:
            self._hold(f"C4 pause reconciliation failure: {exc}")
