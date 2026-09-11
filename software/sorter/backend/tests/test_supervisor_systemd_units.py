from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize(
    "relative_path",
    (
        "software/systemd/sorter-backend.service",
        "software/systemd/sorter-backend-dev.service",
        "software/sorteros/build/overlay/etc/systemd/system/sorter-backend.service",
        "software/sorteros/build/overlay/etc/systemd/system/sorter-backend-dev.service",
    ),
)
def test_systemd_signals_the_supervisor_before_its_backend(relative_path: str) -> None:
    unit = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")

    assert "exec __SOFTWARE_DIR__/sorter/backend/.venv/bin/python3 supervisor.py" in unit
    assert "KillMode=mixed" in unit
    assert "TimeoutStopSec=15" in unit
    assert "uv run python supervisor.py" not in unit
