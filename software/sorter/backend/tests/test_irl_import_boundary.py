"""Fresh-process package boundary and real-object compatibility checks."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

BACKEND = Path(__file__).resolve().parents[1]
SAFETY = '''
import sys
def audit(event, args):
    if event in ('sqlite3.connect', 'socket.connect', 'socket.connect_ex',
                 'socket.bind', 'socket.getaddrinfo'):
        raise AssertionError('Import must not perform I/O: ' + event)
sys.addaudithook(audit)
def no_construction(frame, event, arg):
    if event == 'call':
        path = frame.f_code.co_filename.replace('\\\\', '/')
        name = frame.f_code.co_name
        if any('/' + part + '/' in path for part in ('hardware', 'machine_platform', 'serial')):
            if name == '__init__' or name.startswith(('build_', 'discover_')) or name == 'open':
                raise AssertionError('Import must not construct hardware: ' + name)
sys.setprofile(no_construction)
'''
GUARD = '''
import importlib.abc
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'irl.config' or fullname.split('.')[0] in ('hardware', 'machine_platform'):
            raise AssertionError('Forbidden eager import: ' + fullname)
sys.meta_path.insert(0, Guard())
'''


def run_isolated(code, tmp_path):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               LOCAL_STATE_DB_PATH=str(tmp_path / "forbidden.sqlite"),
               MACHINE_SPECIFIC_PARAMS_PATH=str(tmp_path / "machine.toml"))
    result = subprocess.run([sys.executable, "-c", SAFETY + textwrap.dedent(code)],
                            cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "forbidden.sqlite").exists()


@pytest.mark.parametrize("statement", ["import irl", "import irl.bin_layout"])
def test_pure_import_never_loads_runtime(statement, tmp_path):
    run_isolated(GUARD + statement + '''
assert 'irl.config' not in sys.modules
assert not any(n.split('.')[0] in ('hardware', 'machine_platform') for n in sys.modules)
import irl.bin_layout
assert callable(irl.bin_layout._parseLayersDict)
assert irl.bin_layout._parseLayersDict({'layers': [{'sections': [['medium']]}]})
''', tmp_path)


@pytest.mark.parametrize("name,module", [
    ("IRLConfig", "irl.config"), ("IRLInterface", "irl.config"),
    ("mkIRLConfig", "irl.config"), ("mkIRLInterface", "irl.config"),
    ("StepperMotor", "hardware.sorter_interface"),
    ("ServoMotor", "hardware.sorter_interface"),
])
def test_explicit_export_is_exact_real_object_and_cached(name, module, tmp_path):
    run_isolated(f'''
import importlib
import irl
assert {module!r} not in sys.modules
from irl import {name}
assert {name} is getattr(importlib.import_module({module!r}), {name!r})
loaded = sys.modules[{module!r}]
def forbidden(*args, **kwargs):
    raise AssertionError('Resolved attribute must be cached')
irl.import_module = forbidden
assert getattr(irl, {name!r}) is {name}
assert getattr(irl, {name!r}) is {name}
assert sys.modules[{module!r}] is loaded
''', tmp_path)


def test_unknown_attribute_and_public_introspection(tmp_path):
    run_isolated(GUARD + '''
import irl
assert set(irl.__all__) == {'IRLConfig', 'IRLInterface', 'mkIRLConfig', 'mkIRLInterface',
                            'StepperMotor', 'ServoMotor'}
assert set(irl.__all__).issubset(dir(irl))
try:
    irl.missing_export
except AttributeError:
    pass
else:
    raise AssertionError('Missing attribute did not raise')
''', tmp_path)
