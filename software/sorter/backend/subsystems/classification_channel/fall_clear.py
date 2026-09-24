"""Provisional gravity model for the experimental C4 adapter, not measurement.

Layer numbers here are physical 1..6; BinAddress uses zero-based layer_index.
The contact path starts at rest to conservatively discard impact velocity.
"""
from dataclasses import dataclass
from math import ceil, isfinite, sqrt


@dataclass(frozen=True)
class FallClearModel:
    release_to_chute_inches: float = 2.0
    first_flap_inches: float = 8.0
    layer_pitch_inches: float = 9.0
    flap_path_inches: float = 6.0
    funnel_path_inches: float = 5.5
    effective_acceleration_g: float = 0.30
    fixed_allowance_s: float = 0.15
    round_up_s: float = 0.05
    gravity_m_s2: float = 9.80665

    def __post_init__(self):
        for name, value in vars(self).items():
            if not isfinite(value) or value < 0:
                raise ValueError(f"invalid fall-clear {name}")
        if min(self.effective_acceleration_g, self.round_up_s, self.gravity_m_s2) <= 0:
            raise ValueError("positive acceleration and rounding interval required")

    def seconds(self, layer: int) -> float:
        if type(layer) is not int or not 1 <= layer <= 6:
            raise ValueError("physical chute layer must be 1..6")
        vertical = self.release_to_chute_inches + self.first_flap_inches + self.layer_pitch_inches * (layer - 1)
        contact = self.flap_path_inches + self.funnel_path_inches
        total = (sqrt(2 * vertical * 0.0254 / self.gravity_m_s2)
                 + sqrt(2 * contact * 0.0254 / (self.effective_acceleration_g * self.gravity_m_s2))
                 + self.fixed_allowance_s)
        return ceil(total / self.round_up_s) * self.round_up_s
