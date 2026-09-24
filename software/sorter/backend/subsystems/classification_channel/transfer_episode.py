"""One C3 release and its bounded recovery; never a replacement for C4 ownership."""
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class TransferEpisode:
    boundary_index: int
    pocket_id: int
    started_at_mono: float
    started_at_wall: float
    leader_id: int | None = None
    episode_id: str = field(default_factory=lambda: uuid4().hex)
    followthrough_count: int = 0
    followthrough_issued: bool = False
    followthrough_at_mono: float | None = None
    state: str = "waiting"
    forced_reject_reason: str | None = None
    piece_uuid: str | None = None
    first_pass: bool = False
    group_size_unknown: bool = False
    release_evidence: dict = field(default_factory=dict)
    support: dict = field(default_factory=dict)
    support_motion_deg: float = 0.0
    support_missing_frames: int = 0
    support_last_ts: float = 0.0
    location_lost: bool = False
    recovery_started_mono: float | None = None
    recovery_deadline_mono: float | None = None
    recovery_elapsed_s: float = 0.0
    recovery_stage: int = 0
    recovery_branch: str | None = None
    recovery_legs: list[dict] = field(default_factory=list)
    recovery_decision: dict = field(default_factory=dict)
    recovery_arrival: bool = False
    recovery_success_stage: int | None = None
    terminal_recovery_attempted: bool = False
    terminal_recovery_active: bool = False
    terminal_recovery_leg_offset: int = 0
    terminal_recovery_previous: dict = field(default_factory=dict)

    @property
    def recovery_open(self) -> bool:
        return (self.state in {"waiting", "followthrough", "recovering"} or
                self.state == "unresolved" and self.terminal_recovery_active)

    @property
    def current_recovery_legs(self) -> list[dict]:
        return self.recovery_legs[self.terminal_recovery_leg_offset:]

    @property
    def unresolved(self) -> bool:
        return self.state in {"waiting", "followthrough", "recovering", "unresolved"}
