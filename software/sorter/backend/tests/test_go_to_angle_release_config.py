import tomllib

import pytest
from fastapi import HTTPException

from server.routers.tuning import get_go_to_angle_config, set_go_to_angle_config
from subsystems.feeder.go_to_angle.config import configFromDict


@pytest.fixture
def machine_file(tmp_path, monkeypatch):
    path = tmp_path / "machine.toml"
    path.write_text('[unrelated]\nvalue = "preserve"\n'
                    '[feeder_go_to_angle]\nprecise_pulse_output_deg = 4.5\n'
                    'enable_ch1 = false\nfuture_key = "preserve"\n')
    monkeypatch.setenv("MACHINE_SPECIFIC_PARAMS_PATH", str(path))
    return path


def test_legacy_get_is_read_only_and_exposes_independent_field(machine_file):
    before = machine_file.read_bytes()
    result = get_go_to_angle_config()
    assert result["config"]["ch3_release_margin_output_deg"] == 4.5
    assert any(f["key"] == "ch3_release_margin_output_deg" for f in result["fields"])
    assert machine_file.read_bytes() == before


def test_old_client_c2_edit_preserves_c3_then_explicit_c3_edit_is_independent(machine_file):
    result = set_go_to_angle_config({"precise_pulse_output_deg": 3.0})["config"]
    assert result["precise_pulse_output_deg"] == 3.0
    assert result["ch3_release_margin_output_deg"] == 4.5
    result = set_go_to_angle_config({"ch3_release_margin_output_deg": 6.0})["config"]
    assert result["precise_pulse_output_deg"] == 3.0
    assert result["ch3_release_margin_output_deg"] == 6.0
    # Simulate a later old client saving other controls, followed by a restart/read.
    set_go_to_angle_config({"precise_pulse_output_deg": 2.0})
    saved = tomllib.loads(machine_file.read_text())
    assert saved["unrelated"] == {"value": "preserve"}
    assert saved["feeder_go_to_angle"]["future_key"] == "preserve"
    assert saved["feeder_go_to_angle"]["enable_ch1"] is False
    cfg = configFromDict(saved["feeder_go_to_angle"])
    assert (cfg.precise_pulse_output_deg, cfg.ch3_release_margin_output_deg) == (2.0, 6.0)


@pytest.mark.parametrize("value", [-1, True, None, "bad", float("nan"), float("inf")])
def test_invalid_c3_value_rejects_whole_update_without_writing(machine_file, value):
    before = machine_file.read_bytes()
    with pytest.raises(HTTPException) as exc:
        set_go_to_angle_config({"ch3_release_margin_output_deg": value,
                                "enable_ch1": True})
    assert exc.value.status_code == 400
    assert "finite nonnegative" in exc.value.detail
    assert machine_file.read_bytes() == before


@pytest.mark.parametrize("section,expected", [
    ({}, 3.0),
    ({"precise_pulse_output_deg": 4.5}, 4.5),
    ({"precise_pulse_output_deg": -4.5}, 4.5),
    ({"precise_pulse_output_deg": "bad"}, 3.0),
    ({"precise_pulse_output_deg": 4.5, "ch3_release_margin_output_deg": 0}, 0),
    ({"precise_pulse_output_deg": 4.5, "ch3_release_margin_output_deg": 6}, 6),
])
def test_direct_config_legacy_compatibility(section, expected):
    before = dict(section)
    assert configFromDict(section).ch3_release_margin_output_deg == expected
    assert section == before


def test_first_save_preserves_default_c3_before_c2_change(machine_file):
    machine_file.write_text('[unrelated]\nvalue = "preserve"\n')
    result = set_go_to_angle_config({"precise_pulse_output_deg": 6.0})["config"]
    assert result["ch3_release_margin_output_deg"] == 3.0
    result = set_go_to_angle_config({"ch3_release_margin_output_deg": 0})["config"]
    assert result["ch3_release_margin_output_deg"] == 0
