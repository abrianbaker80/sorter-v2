from dataclasses import replace
from types import SimpleNamespace

import pytest

from subsystems.classification_channel.fall_clear import FallClearModel
from subsystems.classification_channel.demand_planner import Timing, ChuteObservation
from subsystems.classification_channel.physical_binding import PhysicalC4Binding
from subsystems.distribution.chute import BinAddress
from test_demand_planner import at_p0, chute, finish
from test_physical_binding import rig, calibration, Door


def test_authorized_six_layer_bounds():
    model = FallClearModel()
    assert [model.seconds(i) for i in range(1, 7)] == pytest.approx([.85, .95, 1, 1.05, 1.10, 1.15])
    larger = replace(model, fixed_allowance_s=.25)
    assert all(larger.seconds(i) >= model.seconds(i) + .099 for i in range(1, 7))


@pytest.mark.parametrize('layer', [0, 7, True, 1.5])
def test_invalid_layer(layer):
    with pytest.raises(ValueError):
        FallClearModel().seconds(layer)


@pytest.mark.parametrize('kwargs', [dict(effective_acceleration_g=0), dict(round_up_s=0),
    dict(fixed_allowance_s=-1), dict(gravity_m_s2=float('nan'))])
def test_invalid_model(kwargs):
    with pytest.raises(ValueError):
        FallClearModel(**kwargs)


@pytest.mark.parametrize('destination,delay', [('A', .85), ('B', .95), ('C', 1.0), (None, 1.0)])
def test_release_timer_uses_discharged_destination_and_no_extra_cooldown(destination, delay):
    planner, load = at_p0(destination)
    planner.timing = Timing(1, .1, fall_clear_by_destination={'A': .85, 'B': .95, 'C': 1., 'reject': 1.})
    chosen = destination or 'reject'
    decision = planner.plan(12, chute(chosen), drain=True)
    assert len(finish(planner, decision, 13, release=12.6)) == 1
    assert planner.fall_clear_at == pytest.approx(12.6 + delay)
    deadline = planner.fall_clear_at
    assert planner._position('next', deadline-.000001, ChuteObservation(chosen, {'next': 2}))[1] is None
    assert planner._position('next', deadline, ChuteObservation(chosen, {'next': 2}))[1].depart_at == deadline
    assert finish(planner, decision, deadline, release=12.6) == ()
    assert planner.fall_clear_at == deadline


def test_physical_binding_maps_each_bin_layer_and_discard_passthrough(rig):
    _, motor, chute_device, _, _ = rig
    chute_device.layout.layers = [SimpleNamespace(sections=[SimpleNamespace(bins=[0])]) for _ in range(3)]
    binding = PhysicalC4Binding(mode='fail-forward-physical-experimental', c4=motor, chute=chute_device,
        doors={i: Door() for i in range(3)}, destinations={str(i): BinAddress(i, 0, 0) for i in range(3)},
        discard_destination='discard', calibration=calibration(fall_clear_model=FallClearModel()),
        recognize=lambda _: None, routing_timeout_s=1)
    assert dict(binding.runtime.planner.timing.fall_clear_by_destination) == pytest.approx(
        {'0': .85, '1': .95, '2': 1.0, 'discard': 1.0})


def test_mapping_is_snapshot_and_unknown_route_fails_before_fifo_completion():
    planner, _ = at_p0('A')
    values={'reject': 1.0}
    planner.timing=Timing(1, .1, fall_clear_by_destination=values)
    values['A']=.01
    decision=planner.plan(12, chute(), drain=True)
    with pytest.raises(KeyError):
        finish(planner, decision, 13)
    assert planner.fifo.boundary == 6
    assert planner.fifo.pending_index == decision.index
