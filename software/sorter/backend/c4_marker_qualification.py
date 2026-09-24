"""Bounded C4 qualification under the existing exclusive hardware worker.

No controller is created here. The caller lends its one initialized IRL;
normal controller operation and manual admission remain blocked by the
existing worker lifecycle. The positioner owns all signed target trims and confirmation.
"""

from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import statistics
import hashlib
import threading
import time

from fastapi import APIRouter, HTTPException
import server.shared_state as shared
from subsystems.classification_channel.marker_positioner import (
    MarkerMapping,
    MarkerPositioner,
    PositionError,
    PositionLimits,
    TrackedStepperIndexMotor,
    error_deg,
)
from subsystems.classification_channel.marker_source import BridgeMarkerSource
from subsystems.classification_channel.five_sector_platter import C4FiveSectorPlatter

router = APIRouter()
ROOT = Path(__file__).resolve().parent / "c4_marker_positioning"



def prior_result():
    expected = {'qualification.json': '244aea1041cf344d7490f2dd970098ccbde245557f75d77b13d84618ea4be275', 'mapping.json': '155f63e47fc1a4e71dc805f49f2286f93807053c2821ad9eb64a741c5e6caa75'}
    for name, digest in expected.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise PositionError("Prior qualification attribution or mapping changed")
    previous = json.loads((ROOT / "qualification.json").read_text())
    if (previous["status"] != "failed" or not previous["stop_verified"]
            or previous["attempted"] != 1 or previous["passed"] != 0
            or previous["moves"][0]["requested_boundary"] != 1
            or previous["moves"][0]["confirmed_index"] is not None):
        raise PositionError("Prior result does not attribute the empty maintenance rotor to P1")
    return previous


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@router.get("/api/system/c4-marker-qualification")
def status():
    path = ROOT / "signed_qualification.json"
    return json.loads(path.read_text()) if path.exists() else {"status": "not_started"}


@router.post("/api/system/c4-marker-qualification")
def start():
    from server.routers.system import _start_hardware_worker

    with shared.hardware_lifecycle_lock:
        if shared.controller_ref is not None or shared.hardware_state != "initialized":
            raise HTTPException(
                409, "Requires empty ownership, initialized hardware and no controller."
            )
        if (ROOT / "signed_qualification.json").exists():
            raise HTTPException(
                409, "Qualification already attempted; inspect the durable result."
            )
        prior_result()
        return _start_hardware_worker(
            state="initializing",
            step="Qualifying C4 blue-marker indexes",
            success_state="initialized",
            fn=getattr(shared, "_hardware_c4_marker_fn", None),
            busy_message="C4 marker qualification active.",
            missing_fn_message="C4 marker qualification owner unavailable.",
            started_message="Exclusive C4 marker qualification started.",
        )


def stable_phase(window):
    samples = window.samples
    first = samples[0].phase_deg
    return (
        first + statistics.median(error_deg(s.phase_deg, first) for s in samples)
    ) % 360


class TraceMotor(TrackedStepperIndexMotor):
    def __init__(self, stepper, platter, report, persist):
        super().__init__(stepper, platter)
        self.report, self.persist = report, persist
        self.row = None
        self.positioner = None

    def start(self, degrees, speed, token):
        window = self.positioner._window
        pre = stable_phase(window)
        commands = self.row["commands"]
        if commands:
            commands[-1]["post_phase"] = pre
            self.row["initial_residual"] = (
                error_deg(self.row["target_phase"], pre)
                if len(commands) == 1
                else self.row["initial_residual"]
            )
        command = {
            "status": "dispatching",
            "angle": degrees,
            "steps": self.platter.output_degrees_to_motor_microsteps(degrees),
            "pre_phase": pre,
            "source_fence": None,
            "frames": [],
        }
        commands.append(command)
        self.persist()
        receipt = super().start(degrees, speed, token)
        command.update(status="accepted", receipt=asdict(receipt))
        self.persist()
        return receipt


class TraceSource:
    def __init__(self, source, motor):
        self.source, self.motor = source, motor

    def fence(self):
        fence = self.source.fence()
        row = self.motor.row
        if row and row["commands"]:
            row["commands"][-1]["source_fence"] = asdict(fence)
        return fence

    def sample(self):
        sample = self.source.sample()
        row = self.motor.row
        if row and row["commands"] and sample is not None:
            row["commands"][-1]["frames"].append(asdict(sample))
        return sample


def qualify(
    motor,
    source,
    *,
    reference,
    mapping_path,
    persist,
    report,
    stop,
    clock=time.monotonic,
    wait=time.sleep,
):
    """Recover the attributed failed P1 target, then request boundaries 2..10."""
    try:
        previous = prior_result()
        mapping = MarkerMapping.load(mapping_path, source.geometry, motor.coordinate_identity())
        if mapping.zero_phase_deg != reference or mapping.motor_sign != 1:
            raise PositionError("Persisted reference or proven polarity differs")
        report.update(mapping=asdict(mapping), mapping_path=str(mapping_path),
                      polarity=previous["polarity"])
        persist()
        traced_source = TraceSource(source, motor)
        faults = []

        def fault(reason):
            faults.append(reason)
            stop(reason)

        positioner = MarkerPositioner(motor, traced_source, mapping, fault, clock=clock)
        motor.positioner = positioner
        for boundary in range(1, 11):
            row = {
                "requested_boundary": boundary,
                "pocket_index": boundary % 10,
                "target_phase": mapping.phase(boundary),
                "commands": [],
                "confirmed_index": None,
            }
            report["moves"].append(row)
            motor.row = row
            row["recovery"] = boundary == 1
            if boundary == 1:
                positioner.begin_target_trim(1, report["speed"])
            else:
                positioner.request_index(boundary, report["speed"])
            try:
                while (result := positioner.poll()) is None:
                    wait(0.02)
                row["confirmed_index"] = asdict(result)
                row["final_phase"] = result.phase_deg
                row["final_residual"] = result.residual_deg
            finally:
                if positioner._window is not None and positioner._window.samples:
                    final = stable_phase(positioner._window)
                    row["last_observed_phase"] = final
                    if row["recovery"] and not row["commands"]:
                        row["pre_phase"] = final
                        row["initial_residual"] = error_deg(mapping.phase(boundary), final)
                        row["first_post_command_phase"] = None
                    if row["commands"]:
                        row["commands"][-1]["post_phase"] = final
                if row["commands"]:
                    first = row["commands"][0]
                    row["pre_phase"] = first["pre_phase"]
                    initial_post = first.get("post_phase")
                    row["first_post_command_phase"] = initial_post
                    row["initial_residual"] = (
                        error_deg(mapping.phase(boundary), first["pre_phase"] if row["recovery"] else initial_post)
                        if initial_post is not None
                        else None
                    )
                    for command in row["commands"]:
                        post_phase = command.get("post_phase")
                        command["observed_rotation"] = (
                            error_deg(post_phase, command["pre_phase"])
                            if post_phase is not None
                            else None
                        )
                persist()
        report.update(
            status="qualified",
            passed=10,
            attempted=10,
            return_error=error_deg(reference, report["moves"][-1]["final_phase"]),
        )
        persist()
    except Exception as exc:
        report.update(
            status="failed",
            blocker=str(exc),
            passed=sum(bool(r["confirmed_index"]) for r in report["moves"]),
            attempted=len(report["moves"]),
        )
        try:
            stop(str(exc))
            report["stop_verified"] = True
        except Exception as stop_exc:
            report["stop_verified"] = False
            report["stop_error"] = str(stop_exc)
        persist()
        raise


def run(irl, irl_config, gc):
    from server.routers.steppers import _halt_stepper

    if (
        shared.controller_ref is not None
        or shared.getActiveIRL() is not irl
        or shared.hardware_worker_thread is not threading.current_thread()
    ):
        raise PositionError("Exclusive initialized IRL ownership was not transferred")
    if gc.runtime_stats.activeIncident() is not None:
        raise PositionError("Active hardware/runtime incident")
    for name in (
        "carousel_stepper",
        "c_channel_1_rotor_stepper",
        "c_channel_2_rotor_stepper",
        "c_channel_3_rotor_stepper",
        "chute_stepper",
    ):
        stepper = getattr(irl, name, None)
        if stepper is not None and not stepper.stationary_verified():
            raise PositionError(f"{name} is not stopped at qualification admission")
    settings = json.loads((ROOT / "reference.json").read_text())
    reference = settings["zero_phase_deg"]
    if not math.isfinite(reference) or abs(reference - 195.772024) > 0.000001:
        raise PositionError("Accepted marker reference differs")
    report = {
        "status": "running",
        "moves": [],
        "speed": 500,
        "reference": settings,
        "limits": asdict(PositionLimits()),
    }
    with (ROOT / "signed_qualification.json").open("x") as stream:
        json.dump(report, stream)
        stream.flush()
        os.fsync(stream.fileno())

    def persist():
        save(ROOT / "signed_qualification.json", report)

    stepper = irl.carousel_stepper
    platter = C4FiveSectorPlatter.from_irl_config(irl_config)
    motor = TraceMotor(stepper, platter, report, persist)
    source = BridgeMarkerSource(
        settings["base_url"],
        center=tuple(settings["center"]),
        shape=tuple(settings["shape"]),
        camera_id=settings["camera_id"],
    )
    if source.geometry != settings["geometry"]:
        raise PositionError("Camera geometry differs from the accepted reference")
    stopped = False

    def stop(reason):
        nonlocal stopped
        if stopped:
            return
        _halt_stepper(stepper, force=True)
        if not stepper.stationary_verified():
            raise PositionError("C4 fault stop was not verified")
        stopped = True

    prior_result()
    qualify(
        motor,
        source,
        reference=reference,
        mapping_path=ROOT / "mapping.json",
        persist=persist,
        report=report,
        stop=stop,
    )
