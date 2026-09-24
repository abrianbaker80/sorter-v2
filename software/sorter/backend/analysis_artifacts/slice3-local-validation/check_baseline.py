"""Reproduce unrelated failures with baseline modules, without editing checkout."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
sources = {
    "project_harvest_projects": subprocess.check_output([
        "git", "show", "HEAD:software/sorter/backend/project_harvest_projects.py"
    ], cwd=root),
    "hardware.sorter_interface": (root / "analysis_artifacts/slice3-local-before/hardware/sorter_interface.py").read_bytes(),
    "perception.inference": (root / "analysis_artifacts/slice3-local-before/perception/inference.py").read_bytes(),
    "perception.service": (root / "analysis_artifacts/slice3-local-before/perception/service.py").read_bytes(),
}
for name, data in sources.items():
    filename = root / (name.replace(".", "/") + ".py")
    spec = importlib.util.spec_from_loader(name, loader=None, origin=str(filename))
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(filename)
    sys.modules[name] = module
    exec(compile(data, str(filename), "exec"), module.__dict__)
raise SystemExit(pytest.main([
    "tests/test_project_harvest_projects.py",
    "tests/test_stepper_endpoint_safety.py", "-q",
    "perception/tests", "--tb=short",
    "--junitxml=analysis_artifacts/slice3-local-validation/baseline.xml",
]))
