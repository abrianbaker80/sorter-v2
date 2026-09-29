"""Native v0.3.0 custody adapter. Never issues a hardware/transport command."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import replace
import uuid

import smart_bins_native_custody as custody
import smart_bins_native_completion as completion
import smart_bins_service as service


class NativeCustodyAdapter:
    def __init__(self, machine_id: str):
        self.machine_id = machine_id
        self.incarnation = str(uuid.uuid4())
        state = custody.startup_state(machine_id)
        self.active = state["active"]
        self.blocked = state["blocked"]
        if self.active:
            with custody.read_only() as conn:
                try:
                    completion.check_schema(conn)
                except completion.NativeCompletionRefused as exc:
                    raise custody.CustodyRefused(
                        "Active native custody requires explicit RB04 completion initialization"
                    ) from exc
                self.blocked = self.blocked or completion.current_blocker_on_connection(
                    conn, machine_id)
        self.settings = state.get("settings")
        self.current = None
        self.permit = None
        self.route = None
        self.transport = None
        self._fault = None
        self.profiler = None

    def _timed(self, name):
        profiler = self.profiler
        return profiler.timer(name) if profiler is not None and profiler.enabled else nullcontext()

    def _current_blocker(self, conn=None, except_reservation=None):
        with self._timed("smart_bins.current_authorization_ms"):
            if conn is None:
                return completion.current_blocker(self.machine_id, except_reservation)
            return completion.current_blocker_on_connection(
                conn, self.machine_id, except_reservation
            )

    def require_startup(self):
        if self.blocked or self._fault:
            raise custody.CustodyRefused(
                "Unresolved Smart Bin custody blocks startup motion"
            )
        if self.active:
            with custody.read_only() as conn:
                if custody.has_blocker(conn, self.machine_id):
                    raise custody.CustodyRefused("Durable claims block startup motion")

    def check_motion(self, kind, role, scope):
        # C1/C2 do not query Smart Bin state. C3 admission is separate from release.
        if role in ("c_channel_1_rotor", "c_channel_2_rotor"):
            return
        if not self.active:
            self.require_startup()
            return
        if self._fault or self.blocked:
            raise custody.CustodyRefused("Smart Bin custody requires reconciliation")
        with custody.read_only() as conn:
            except_id = (
                self.current.reservation_id
                if (self.current and scope and scope.get("adapter") is self
                    and scope.get("kind") in ("route", "dispatch"))
                else None
            )
            if self._current_blocker(conn, except_id):
                raise custody.CustodyRefused(
                    "Current Smart Bin completion obligation blocks motion"
                )
        if scope and scope.get("adapter") is self and scope.get("kind") == "dispatch":
            if kind == "finite" and role in (
                "carousel",
                "classification_channel_rotor",
                "c_channel_4_rotor",
            ):
                if scope.get("remaining") != 1:
                    raise custody.CustodyRefused("One-shot dispatch already consumed")
                scope["remaining"] = 0
                return
            if kind == "enable":
                return
            raise custody.CustodyRefused(
                "Dispatch permit does not authorize another motion"
            )
        with custody.read_only() as conn:
            except_id = None
            if (
                scope
                and scope.get("adapter") is self
                and scope.get("kind") == "route"
                and self.current
            ):
                row = custody._current(conn, self.current)
                if (
                    row["status"] != "RESERVED"
                    or row["reservation_state"] != "RESERVED"
                ):
                    raise custody.CustodyRefused(
                        "Intended route is frozen after release intent"
                    )
                except_id = self.current.reservation_id
            if custody.has_blocker(conn, self.machine_id, except_id):
                raise custody.CustodyRefused(
                    "Unresolved Smart Bin claim blocks release, admission and route motion"
                )

    def admission_allowed(self):
        try:
            with custody.motion_entry("admission", "c_channel_3_rotor"):
                return True
        except custody.CustodyRefused:
            return False

    def reserve_route(self, piece, category, profile, layout, chute, transport):
        """RB02 alone selects destination. All kinematic reads precede DB writes."""
        with custody.MOTION_LOCK:
            if self.blocked or self._fault:
                raise custody.CustodyRefused("Native custody is blocked")
            if self.current:
                if (
                    self.current.piece_uuid != piece.uuid
                    or self.current.head_generation != transport.native_head_generation
                ):
                    raise custody.CustodyRefused(
                        "Previous native owner remains unresolved"
                    )
                return self.route
            with custody.read_only() as conn:
                custody._check(conn)
                if custody.has_blocker(conn, self.machine_id):
                    raise custody.CustodyRefused(
                        "Existing durable custody blocks reservation"
                    )
                if self._current_blocker(conn):
                    raise custody.CustodyRefused(
                        "Current completion obligation blocks reservation"
                    )
                settings = conn.execute(
                    "SELECT * FROM smart_bin_native_settings WHERE machine_id=?",
                    (self.machine_id,),
                ).fetchone()
                policy = conn.execute(
                    "SELECT * FROM smart_bin_policy_revisions WHERE machine_id=? AND id=?",
                    (self.machine_id, settings["policy_id"]),
                ).fetchone()
                groups = conn.execute(
                    "SELECT id FROM smart_bin_group_keys WHERE kind='category' AND namespace=? AND part_id=? AND provenance='known'",
                    (settings["namespace"], category),
                ).fetchall()
                sessions = conn.execute(
                    "SELECT id FROM sorting_sessions WHERE machine_id=? AND status='active'",
                    (self.machine_id,),
                ).fetchall()
                machine_rev = conn.execute(
                    "SELECT state_revision FROM smart_bin_machines WHERE machine_id=?",
                    (self.machine_id,),
                ).fetchone()[0]
                slots = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT s.*,c.id AS cycle_id,c.provenance FROM smart_bin_slots s JOIN smart_bin_cycles c ON c.slot_id=s.id AND c.machine_id=s.machine_id WHERE s.machine_id=? AND c.closed_at IS NULL",
                        (self.machine_id,),
                    )
                ]
                config_digest = service.configuration_digest_on_connection(conn)
            if (
                policy is None
                or policy["compiled_artifact_hash"] != profile.artifact_hash
                or len(groups) != 1
                or len(sessions) != 1
            ):
                raise custody.CustodyRefused(
                    "Native policy, category or active session is not uniquely qualified"
                )
            from subsystems.distribution.chute import BinAddress

            qualified = []
            for slot in slots:
                li, si, bi = (
                    slot[k] for k in ("layer_index", "section_index", "bin_index")
                )
                try:
                    layer = layout.layers[li]
                    section = layer.sections[si]
                    bin_ = section.bins[bi]
                except (IndexError, AttributeError):
                    raise custody.CustodyRefused(
                        "Native layout disagrees with qualified slots"
                    ) from None
                qualified.append(
                    service.SlotQualification(
                        slot["id"],
                        slot["cycle_id"],
                        slot["layout_revision"],
                        bool(layer.enabled and section.enabled),
                        bool(chute.isBinReachable(BinAddress(li, si, bi))),
                        bool(bin_.not_in_inventory),
                        layer.max_pieces_per_bin,
                        layer.max_dimension_mm,
                        "native-cycle:" + slot["cycle_id"],
                    )
                )
            qualification = service.RoutingQualification(
                self.machine_id,
                settings["policy_id"],
                profile.artifact_hash,
                settings["routing_revision"],
                machine_rev,
                config_digest,
                "native:" + self.incarnation,
                False,
                tuple(qualified),
            )
            request = service.ReservationRequest(
                piece.uuid,
                f"{self.incarnation}:{transport.native_head_generation}",
                sessions[0][0],
                groups[0][0],
                not_in_inventory=piece.not_in_inventory is True,
                max_dimension_mm=piece.max_dimension_mm,
                too_big=piece.too_big,
            )
            with self._timed("smart_bins.reserve_ms"):
                result = service.reserve(
                    request,
                    qualification,
                    expected_state_revision=machine_rev,
                    expected_qualification_hash=service.qualification_digest(qualification),
                    request_key="native-reserve:" + request.route_attempt,
                )
            if result["code"] != "OK":
                raise custody.CustodyRefused(
                    "Native reservation refused: " + result["code"]
                )
            identity = custody.NativeIdentity(
                self.machine_id,
                piece.uuid,
                result["reservation_id"],
                self.incarnation,
                transport.native_head_generation,
                settings["routing_revision"],
                "",
            )
            try:
                custody.bind_reservation(identity, config_digest)
            except Exception:
                self._fault = "Reservation committed but native custody binding failed"
                raise
            if (piece.native_machine_id not in (None, identity.machine_id)
                or piece.native_reservation_id not in (None, identity.reservation_id)):
                self._fault = "KnownObject native provenance conflicts with durable custody"
                raise custody.CustodyRefused(self._fault)
            piece.native_machine_id = identity.machine_id
            piece.native_reservation_id = identity.reservation_id
            self.current = identity
            self.transport = transport
            selected = next((s for s in slots if s["id"] == result["slot_id"]), None)
            self.route = {
                "reservation_id": identity.reservation_id,
                "kind": result["destination_kind"],
                "address": BinAddress(
                    selected["layer_index"],
                    selected["section_index"],
                    selected["bin_index"],
                )
                if selected
                else None,
                "config_digest": config_digest,
            }
            return self.route

    @contextmanager
    def route_scope(self):
        with custody.owned_scope({"adapter": self, "kind": "route"}):
            yield

    def arm_ready(self, positioned_uuid, evidence):
        with custody.MOTION_LOCK:
            if self.blocked or self._fault:
                raise custody.CustodyRefused("Native custody is blocked")
            if not self.current or not self.transport:
                raise custody.CustodyRefused("No reserved native head")
            self._verify_head(positioned_uuid)
            if self.permit is not None:
                raise custody.CustodyRefused("READY cannot create another permit")
            if self._current_blocker(
                except_reservation=self.current.reservation_id
            ):
                raise custody.CustodyRefused("Current completion obligation blocks READY")
            identity = replace(self.current, attempt_id=str(uuid.uuid4()))
            digest = service.configuration_digest()
            with self._timed("smart_bins.release_intent_ms"):
                custody.arm(identity, digest, evidence)
            self.current = identity
            self.permit = identity

    def _verify_head(self, positioned_uuid):
        piece = self.transport.getPieceForDistributionPositioning()
        if (
            piece is None
            or piece.uuid != positioned_uuid
            or positioned_uuid != self.current.piece_uuid
            or self.transport.native_head_generation != self.current.head_generation
        ):
            raise custody.CustodyRefused("Native head changed before dispatch")

    @contextmanager
    def dispatch(self, piece_uuid):
        with custody.MOTION_LOCK:
            if self.blocked or self._fault:
                raise custody.CustodyRefused("Native custody is blocked")
            if self.permit is None or self.current != self.permit:
                raise custody.CustodyRefused("No live one-shot dispatch permit")
            self._verify_head(piece_uuid)
            permit = self.permit
            self.permit = None  # failures never make permission replayable
            try:
                with self._timed("smart_bins.dispatch_consume_ms"):
                    custody.consume(permit, service.configuration_digest())
            except Exception:
                self._fault = "Dispatch persistence failed"
                raise
            with custody.owned_scope(
                {"adapter": self, "kind": "dispatch", "remaining": 1}
            ):
                yield permit

    def command_outcome(self, receipt):
        outcome = receipt.outcome
        kind = {"ACCEPTED": "MOTOR_ACCEPTED", "REJECTED": "MOTOR_REJECTED"}.get(
            outcome, "MOTOR_AMBIGUOUS"
        )
        try:
            custody.observe(
                self.current,
                kind,
                {
                    "command_generation": receipt.generation,
                    "requested_steps": receipt.requested_steps,
                    "encoder_present": False,
                    "proves_piece_displacement": False,
                },
                uncertain=kind == "MOTOR_AMBIGUOUS",
            )
            if kind == "MOTOR_AMBIGUOUS":
                self.blocked = True
        except Exception:
            self._fault = (
                "Command outcome persistence failed; release remains unresolved"
            )
            raise

    def observation(self, kind, details=None, *, uncertain=False):
        if not self.active or self.current is None:
            return
        try:
            custody.observe(self.current, kind, details or {}, uncertain=uncertain)
            if uncertain:
                self.blocked = True
        except Exception:
            self._fault = "Native observation persistence failed"
            raise

    def observed_clear(self):
        if self.active:
            custody.machine_observation(
                self.machine_id,
                self.incarnation,
                "CHANNEL_CLEAR_OBSERVED",
                {
                    "exact_piece_identity": None,
                    "quantity": None,
                    "actual_destination": None,
                    "receiving_evidence": False,
                },
            )

    def refuse_unqualified_motion(self, kind, details):
        """Unknown quantity is a durable hold, never an invented one-piece claim."""
        if not self.active:
            return
        if self.blocked or self._fault:
            raise custody.CustodyRefused(
                "Existing custody hold requires reconciliation"
            )
        try:
            custody.hold(self.machine_id, kind, details)
            if self.current:
                self.observation(
                    kind if kind in custody.EVIDENCE_TYPES else "ROUTE_UNCERTAIN",
                    details,
                    uncertain=True,
                )
        finally:
            self.blocked = True
        raise custody.CustodyRefused(
            "Unknown/resident custody requires explicit reconciliation"
        )

    def release_completed(self, piece_uuid, reservation_id, delivery_id):
        """Forget only a fully published, verified owner. Grants no motion."""
        with custody.MOTION_LOCK:
            if (self.current is None or self.current.piece_uuid != piece_uuid
                or self.current.reservation_id != reservation_id):
                raise custody.CustodyRefused("Completion does not own current head")
            completion.verify_native_identity(self.machine_id, reservation_id,
                                              piece_uuid, delivery_id)
            for effect in completion.EFFECTS[:-1]:
                if completion.effect_phase(self.machine_id, reservation_id,
                                           piece_uuid, delivery_id, effect) != "SUCCEEDED":
                    raise custody.CustodyRefused("Completion effect is unfinished")
            if self._current_blocker(except_reservation=reservation_id):
                raise custody.CustodyRefused("Current same-machine blocker remains")
            self.current = None
            self.permit = None
            self.route = None
            self.transport = None


def attach(gc):
    adapter = getattr(gc, "smart_bins_native_adapter", None)
    if adapter is None:
        adapter = NativeCustodyAdapter(gc.machine_id)
        gc.smart_bins_native_adapter = adapter
    adapter.profiler = getattr(gc, "profiler", None)
    custody.install(adapter if adapter.active or adapter.blocked else None)
    return adapter
