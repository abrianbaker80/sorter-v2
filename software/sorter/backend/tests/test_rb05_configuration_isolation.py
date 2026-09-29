"""Synthetic live-style configuration and ordinary import boundaries for RB05."""

import tomllib

from irl.config import (
    CLASSIFICATION_CHANNEL_FLOW,
    FEEDER_FLOW,
    MACHINE_SETUP,
    mkIRLConfig,
)
from test_irl_import_boundary import run_isolated
from toml_config import getPulsePerceptionConfig


def test_legacy_selection_and_feeder_sections_cannot_change_native_flow(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.toml"
    path.write_text(
        '[machine_setup]\n'
        'type = "go_to_angle"\n'
        'feeder = "constant_movement"\n'
        '[feeder_go_to_angle]\n'
        'move_speed_usteps_per_s = 1\n'
        '[feeder_constant_movement]\n'
        'move_speed_usteps_per_s = 2\n'
        '[feeder_pulse_perception]\n'
        'move_speed_usteps_per_s = 2600\n'
        'max_move_output_deg = 80.0\n'
        'ch2_move_speed_usteps_per_s = 900\n'
        'ch3_max_move_output_deg = 30.0\n'
        '[cameras]\n'
        'c_channel_2 = 0\n'
        'c_channel_3 = 1\n'
        'carousel = 2\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(path))
    assert (MACHINE_SETUP, FEEDER_FLOW, CLASSIFICATION_CHANNEL_FLOW) == (
        "classification_channel", "pulse_perception_rev01", "two_piece_state_machine_rev01"
    )
    config = mkIRLConfig()
    assert [config.c_channel_2_camera.device_index,
            config.c_channel_3_camera.device_index,
            config.carousel_camera.device_index] == [0, 1, 2]
    assert config.c_channel_4_rotor_stepper is config.carousel_stepper
    pulse = getPulsePerceptionConfig()
    assert [pulse[f"ch{n}_move_speed_usteps_per_s"] for n in (1, 2, 3)] == [2600, 900, 2600]
    assert [pulse[f"ch{n}_max_move_output_deg"] for n in (1, 2, 3)] == [80.0, 80.0, 30.0]
    assert "move_speed_usteps_per_s" not in pulse
    assert "max_move_output_deg" not in pulse


def test_camera_alias_conflict_is_visible_and_canonical_wins(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.toml"
    path.write_text('[cameras]\nclassification_channel = 3\ncarousel = 7\n', encoding="utf-8")
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(path))
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["cameras"]["classification_channel"] != raw["cameras"]["carousel"]
    assert mkIRLConfig().carousel_camera.device_index == 3


def test_ordinary_native_adapter_import_and_absent_construction_exclude_harvest(tmp_path):
    run_isolated('''
import importlib.abc
class NoHarvest(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('project_harvest_') or fullname in (
            'harvest_integration_storage', 'smart_bins_harvest_integration'):
            raise AssertionError('Harvest application side imported: ' + fullname)
sys.meta_path.insert(0, NoHarvest())
from subsystems.classification_channel.smart_bins_native_adapter import NativeCustodyAdapter
adapter = NativeCustodyAdapter('synthetic-machine')
assert not adapter.active and not adapter.blocked
assert not any(name.startswith('project_harvest_') for name in sys.modules)
''', tmp_path)
