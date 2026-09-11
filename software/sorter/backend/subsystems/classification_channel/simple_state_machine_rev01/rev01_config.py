from dataclasses import dataclass


@dataclass
class Rev01Config:
    rotate_speed_usteps_per_s: int = 7000
    # Active-path UNUSED: the reverse capture-at-rest flow no longer sweeps the
    # carousel while photographing. Kept only for the legacy non-perception
    # fallback (a single fixed move).
    capture_sweep_output_deg: float = 180.0
    # How long CAPTURING photographs the piece AT REST before spawning the
    # Brickognize request and starting the reverse move to the precise zone. The
    # burst deliberately over-captures; crop_quality.selectBurstIndices decides
    # afterwards which frames are valid. The perception stream is capped below
    # the source camera rate, so this window allows the final settled detector
    # view to arrive without adding delay when the frame ceiling is reached.
    capture_at_rest_ms: float = 650.0
    # Speed for the fixed five-pocket move to indexed safe staging.
    precise_converge_speed_usteps_per_s: int = 5000
    # Retained for Two Piece mode and configuration-file compatibility. The
    # conservative one-piece path never steers C4 from a camera COM tolerance.
    precise_center_tolerance_deg: float = 4.0
    # Legacy fixed discharge kick (only used on the non-perception fallback path).
    # The active one-piece path uses the absolute seven-pocket exit target.
    kick_off_output_deg: float = 180.0
    discharge_speed_usteps_per_s: int = 5000
    crop_padding_px: int = 15
    # Hard ceiling on distinct perception frames grabbed at rest. Reaching it
    # ends the burst immediately, before capture_at_rest_ms.
    max_captures: int = 6
    # Ceiling on burst frames sent to Brickognize. All geometrically valid views
    # are retained up to this limit; sharpness ranks them only if the limit is
    # lower than the number available.
    classify_burst_count: int = 6
    # Alongside the "combined" call, fire an extra single-image Brickognize
    # request IN PARALLEL and keep whichever result scores highest. These are
    # redundant, not sequential retries: a lone clean frame frequently recognizes
    # a piece the combined set confuses, and firing every variant concurrently
    # costs the same wall-clock as the slowest single call. A single-image request
    # that would duplicate the combined call (e.g. combined is already one burst
    # frame) is skipped.
    # single_burst: also send just the last (most-settled) C4 burst frame, alone.
    classify_parallel_single_burst: bool = True
    rotate_timeout_s: float = 30.0
    classify_timeout_s: float = 30.0
    presence_streak_to_start: int = 2
    empty_streak_to_abort: int = 3
    # Consecutive zero-piece reads in IDLE required before declaring the channel
    # clear and opening the C3->C4 feed gate. Without this a single detector
    # dropout (the bbox blinks off for a frame while a piece is still there)
    # flips ready=True and C3 pushes a second piece in → double feed. Symmetric
    # to presence_streak_to_start on the arrival side.
    idle_clear_confirm_reads: int = 3
    stuck_in_exit_zone_timeout_s: float = 30.0
    home_offset_output_deg: float = 22.0
    # Legacy non-perception fallback only: pause after the fixed kick-off move
    # before returning to IDLE so the carousel settles.
    post_discharge_pause_ms: float = 300.0

    # Retained for Two Piece mode and configuration-file compatibility. The
    # conservative one-piece path performs one absolute indexed exit move.
    discharge_center_tolerance_deg: float = 3.0
    discharge_max_move_output_deg: float = 270.0
    # One overall budget for the whole discharge of a piece-set, NOT reset per
    # move. When it runs out with the channel still occupied, raise the stuck
    # incident and hold (works from anywhere on the channel, not just the exit).
    discharge_total_timeout_ms: int = 30000
    # How long the whole one-piece C4 channel must read clear CONTINUOUSLY before
    # we commit the drop. The detector blinks, so the window must be unbroken - a
    # single occupied frame resets it.
    discharge_clear_confirm_ms: int = 500
    # Consecutive distinct-frame reads with >=2 on-channel pieces required
    # before latching a multi-feed (which forces the whole cycle to MISC). The
    # detector regularly splits one piece into two boxes or emits a one-frame
    # spurious second box; a single such frame used to mis-flag a multi-drop.
    # Mirror the clear-confirm debounce so one noisy frame can't trip it.
    multi_feed_confirm_reads: int = 3

    # Jitter unstick: the ONLY trigger. If a piece sits in the FALL-OFF region
    # (the exit-only sub-arc, NOT the precise staging band — perception's
    # ``in_exit_majority``) continuously for this long, shake it loose. A piece
    # that drops on its own is in the fall-off for only a frame or two, so it
    # never reaches this; only a genuinely parked piece does.
    discharge_jitter_dwell_ms: int = 250

    # Verifying-discharge: after the move-to-angle settles, wait this long
    # before the first exit-zone re-check, then on stuck run up to N jitter
    # attempts using the shared jitter sequence.
    verify_discharge_wait_ms: int = 1000
    verify_discharge_max_jitter_attempts: int = 3
    jitter_pause_ms: int = 350
    jitter_amplitude_motor_deg: float = 8.0
    jitter_cycles: int = 6
    jitter_speed_usteps_per_s: int = 4000
    jitter_accel_usteps_per_s2: int = 80000


_DEFAULTS = Rev01Config()

FIELD_META: list[dict] = [
    {"key": "rotate_speed_usteps_per_s", "label": "Rotate speed (µsteps/s)", "type": "int", "default": _DEFAULTS.rotate_speed_usteps_per_s, "description": "Motor speed for normal C4 platter rotation (microsteps per second). Higher moves pieces faster but can fling light pieces."},
    {"key": "capture_sweep_output_deg", "label": "Capture sweep (output deg, legacy)", "type": "float", "default": _DEFAULTS.capture_sweep_output_deg, "description": "Legacy non-perception fallback only: single fixed move used to sweep a piece past the camera. Unused on the active perception path."},
    {"key": "capture_at_rest_ms", "label": "Capture-at-rest window (ms)", "type": "float", "default": _DEFAULTS.capture_at_rest_ms, "description": "Maximum time allowed for distinct perception frames to capture the final settled view. The burst ends sooner as soon as its frame ceiling is reached."},
    {"key": "precise_converge_speed_usteps_per_s", "label": "Safe-staging move speed (µsteps/s)", "type": "int", "default": _DEFAULTS.precise_converge_speed_usteps_per_s, "description": "Motor speed for the fixed five-pocket move from C3 intake to indexed safe staging."},
    {"key": "precise_center_tolerance_deg", "label": "Camera converge tolerance (Two Piece only)", "type": "float", "default": _DEFAULTS.precise_center_tolerance_deg, "description": "Used by Two Piece mode; the conservative one-piece path uses absolute pocket positions."},
    {"key": "kick_off_output_deg", "label": "Kick-off move (legacy fallback)", "type": "float", "default": _DEFAULTS.kick_off_output_deg, "description": "Legacy non-perception fallback only. The active one-piece path uses its absolute seven-pocket exit target."},
    {"key": "discharge_speed_usteps_per_s", "label": "Discharge speed (µsteps/s)", "type": "int", "default": _DEFAULTS.discharge_speed_usteps_per_s, "description": "Motor speed for discharge moves (driving the piece into the fall-off zone)."},
    {"key": "crop_padding_px", "label": "Crop padding (px)", "type": "int", "default": _DEFAULTS.crop_padding_px, "description": "Extra pixels added around the detected bounding box when cropping the piece image sent to classification."},
    {"key": "max_captures", "label": "Burst frames to grab per piece (hard ceiling)", "type": "int", "default": _DEFAULTS.max_captures, "description": "Most distinct perception frames grabbed for one piece. Reaching this ceiling ends the capture window immediately."},
    {"key": "classify_burst_count", "label": "Burst frames to use for classification (ceiling)", "type": "int", "default": _DEFAULTS.classify_burst_count, "description": "Most burst frames sent to Brickognize. All geometrically valid views are retained up to this ceiling; sharpness ranks them only when there are more valid views than fit."},
    {"key": "classify_parallel_single_burst", "label": "Also classify the last burst frame alone, in parallel (keep best)", "type": "bool", "default": _DEFAULTS.classify_parallel_single_burst, "description": "Alongside the combined multi-image request, also send just the last burst frame as its own Brickognize call and keep whichever result scores highest. A lone clean frame often recognizes a piece the fused set confuses; costs no extra wall-clock."},
    {"key": "rotate_timeout_s", "label": "Rotate timeout (s)", "type": "float", "default": _DEFAULTS.rotate_timeout_s, "description": "Stop and hold with an incident if C4 cannot prove the piece reached the precise staging zone within this long."},
    {"key": "classify_timeout_s", "label": "Classify timeout (s)", "type": "float", "default": _DEFAULTS.classify_timeout_s, "description": "Give up on the Brickognize classification request after this long; the piece is sent to MISC."},
    {"key": "presence_streak_to_start", "label": "Presence streak to start rotation", "type": "int", "default": _DEFAULTS.presence_streak_to_start, "description": "Consecutive frames a piece must be detected before the channel starts processing it. Filters one-frame detector blips."},
    {"key": "empty_streak_to_abort", "label": "Empty streak to abort rotation", "type": "int", "default": _DEFAULTS.empty_streak_to_abort, "description": "Consecutive distinct empty frames before concluding that C4 lost the piece and holding with an incident."},
    {"key": "idle_clear_confirm_reads", "label": "Idle: zero-read streak to confirm clear (open feed gate)", "type": "int", "default": _DEFAULTS.idle_clear_confirm_reads, "description": "Consecutive zero-piece reads in IDLE required before declaring the channel clear and letting C3 feed the next piece. Guards against a one-frame detector dropout causing a double feed."},
    {"key": "stuck_in_exit_zone_timeout_s", "label": "Stuck-in-exit-zone warn timeout (s)", "type": "float", "default": _DEFAULTS.stuck_in_exit_zone_timeout_s, "description": "If a piece sits in the exit zone this long without dropping, warn the operator."},
    {"key": "home_offset_output_deg", "label": "Home offset (output deg)", "type": "float", "default": _DEFAULTS.home_offset_output_deg, "description": "Offset from the homing sensor position to the channel's actual zero, in output degrees."},
    {"key": "post_discharge_pause_ms", "label": "Post-discharge pause (ms)", "type": "float", "default": _DEFAULTS.post_discharge_pause_ms, "description": "Legacy non-perception fallback only: pause after the fixed kick-off move before returning to IDLE so the carousel settles."},
    {"key": "discharge_center_tolerance_deg", "label": "Discharge camera tolerance (Two Piece only)", "type": "float", "default": _DEFAULTS.discharge_center_tolerance_deg, "description": "Used by Two Piece mode; the conservative one-piece path does not camera-steer discharge."},
    {"key": "discharge_max_move_output_deg", "label": "Max camera converge move (Two Piece only)", "type": "float", "default": _DEFAULTS.discharge_max_move_output_deg, "description": "Used by Two Piece mode; the conservative one-piece path uses one absolute indexed exit move."},
    {"key": "discharge_total_timeout_ms", "label": "Discharge: total budget before hold (ms)", "type": "int", "default": _DEFAULTS.discharge_total_timeout_ms, "description": "One overall time budget for discharge. If it expires without confirmed post-motion clear, stop and hold without crediting the piece."},
    {"key": "discharge_clear_confirm_ms", "label": "Discharge: continuous-clear window to confirm drop (ms)", "type": "int", "default": _DEFAULTS.discharge_clear_confirm_ms, "description": "How long distinct post-motion frames must show the staged C4 exit/precise region continuously clear before the owned piece counts as dropped."},
    {"key": "multi_feed_confirm_reads", "label": "Multi-feed: frames of >=2 pieces to confirm", "type": "int", "default": _DEFAULTS.multi_feed_confirm_reads, "description": "Consecutive distinct frames showing 2+ pieces on the channel before latching a multi-feed (which sends the whole cycle to MISC). Stops a one-frame split detection from mis-flagging."},
    {"key": "discharge_jitter_dwell_ms", "label": "Discharge: dwell in fall-off region before jitter (ms)", "type": "int", "default": _DEFAULTS.discharge_jitter_dwell_ms, "description": "If a piece sits continuously in the fall-off region this long, it's parked — shake it loose with a jitter. A piece dropping normally is only there for a frame or two, so it never triggers this."},
    {"key": "verify_discharge_wait_ms", "label": "Verify-discharge: settle wait before re-check (ms)", "type": "int", "default": _DEFAULTS.verify_discharge_wait_ms, "description": "After a discharge move settles, wait this long before the first exit-zone re-check."},
    {"key": "verify_discharge_max_jitter_attempts", "label": "Verify-discharge: max jitter attempts", "type": "int", "default": _DEFAULTS.verify_discharge_max_jitter_attempts, "description": "If the piece still reads stuck after the settle wait, run up to this many jitter attempts before holding with an incident."},
    {"key": "jitter_pause_ms", "label": "Jitter: pause between attempts (ms)", "type": "int", "default": _DEFAULTS.jitter_pause_ms, "description": "Pause between jitter attempts, giving the piece time to fall before shaking again."},
    {"key": "jitter_amplitude_motor_deg", "label": "Jitter amplitude (motor deg)", "type": "float", "default": _DEFAULTS.jitter_amplitude_motor_deg, "description": "Size of each back-and-forth jitter oscillation, in motor degrees."},
    {"key": "jitter_cycles", "label": "Jitter cycles per burst", "type": "int", "default": _DEFAULTS.jitter_cycles, "description": "Back-and-forth oscillations per jitter attempt."},
    {"key": "jitter_speed_usteps_per_s", "label": "Jitter speed (\u00b5steps/s)", "type": "int", "default": _DEFAULTS.jitter_speed_usteps_per_s, "description": "Motor speed during jitter oscillations."},
    {"key": "jitter_accel_usteps_per_s2", "label": "Jitter accel (\u00b5steps/s\u00b2)", "type": "int", "default": _DEFAULTS.jitter_accel_usteps_per_s2, "description": "Motor acceleration during jitter oscillations — high, so the shake is sharp enough to unstick a piece."},
]


def configFromDict(d: dict) -> Rev01Config:
    cfg = Rev01Config()
    for meta in FIELD_META:
        k = meta["key"]
        if k not in d:
            continue
        raw = d[k]
        try:
            if meta["type"] == "int":
                setattr(cfg, k, int(raw))
            elif meta["type"] == "bool":
                setattr(cfg, k, bool(raw))
            else:
                setattr(cfg, k, float(raw))
        except (TypeError, ValueError):
            pass
    return cfg


def configToDict(cfg: Rev01Config) -> dict:
    return {meta["key"]: getattr(cfg, meta["key"]) for meta in FIELD_META}
