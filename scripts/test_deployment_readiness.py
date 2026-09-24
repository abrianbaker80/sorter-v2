"""Offline tests: fake time/HTTP/processes; no SSH, service or sorter access."""
import ast
import hashlib
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock
import urllib.error

from deployment_readiness import (
    EstablishedStartupFailure, ObservationUnavailable, OperationalRisk,
    TRANSIENT_READ_ERRORS, wait_for_startup,
)

ARTIFACTS = Path(__file__).resolve().parents[2] / 'analysis_artifacts'
WORKER = ARTIFACTS / 'c3-c4-fresh-deployment-20260912/resume_remote.py'


def functions(*names, **namespace):
    tree = ast.parse(WORKER.read_text())
    tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(tree, str(WORKER), 'exec'), namespace)
    return namespace


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.calls = []
        self.runtime = {'payload': {'is_running': False, 'transfer_throughput': {}}}
        self.supervisor = {'backend_pid': 10, 'backend_running': True,
                           'backend_healthy': True, 'restart_requested': False}

    def sleep(self, seconds):
        self.now += seconds

    def api(self, path, port=8000, timeout=5):
        self.calls.append((path, timeout))
        if port == 8001:
            return self.supervisor
        if path == '/health':
            return {'status': 'ok'}
        return self.runtime

    def wait(self, api=None):
        return wait_for_startup(api or self.api, lambda pid: {'pid': pid},
                                clock=lambda: self.now, sleep=self.sleep)

    def test_three_consecutive_successes(self):
        self.assertEqual(self.wait(), {'pid': 10})
        self.assertEqual(self.now, 4)

    def test_refused_listener_despite_healthy_supervisor_recovers(self):
        def api(path, port=8000, **kw):
            if path == '/runtime-stats' and self.now < 8:
                raise urllib.error.URLError('connection refused')
            return self.api(path, port, **kw)
        self.assertEqual(self.wait(api), {'pid': 10})
        self.assertEqual(self.now, 12)

    def test_incomplete_telemetry_recovers(self):
        def api(path, port=8000, **kw):
            if path == '/runtime-stats' and self.now < 6:
                return {'http_status': 503}
            return self.api(path, port, **kw)
        self.assertEqual(self.wait(api), {'pid': 10})
        self.assertEqual(self.now, 10)

    def test_isolated_previous_crash_allowed(self):
        self.supervisor['consecutive_fast_crashes'] = 1
        self.assertEqual(self.wait(), {'pid': 10})

    def test_one_restart_requalifies(self):
        def api(path, port=8000, **kw):
            self.supervisor['backend_pid'] = 10 if self.now < 2 else 11
            return self.api(path, port, **kw)
        self.assertEqual(self.wait(api), {'pid': 11})
        self.assertEqual(self.now, 6)

    def test_repeated_restarts_fail(self):
        def api(path, port=8000, **kw):
            self.supervisor['backend_pid'] = 10 + int(self.now)
            return self.api(path, port, **kw)
        with self.assertRaises(EstablishedStartupFailure):
            self.wait(api)

    def test_reported_crash_loop_fails(self):
        self.supervisor['consecutive_fast_crashes'] = 2
        with self.assertRaises(EstablishedStartupFailure):
            self.wait()

    def test_persistent_listener_failure_uses_full_grace(self):
        def api(path, port=8000, **kw):
            if port == 8000:
                raise urllib.error.URLError('connection refused')
            return self.api(path, port, **kw)
        with self.assertRaises(EstablishedStartupFailure):
            self.wait(api)
        self.assertEqual(self.now, 120)

    def test_supervisor_unavailable_does_not_authorize_rollback(self):
        with self.assertRaises(ObservationUnavailable):
            self.wait(Mock(side_effect=TimeoutError('no observation')))
        self.assertEqual(self.now, 120)

    def test_single_late_failure_is_not_established_failure(self):
        def api(path, port=8000, **kw):
            if self.now < 118:
                raise TimeoutError('supervisor unavailable')
            if port == 8000:
                raise TimeoutError('one failed probe')
            return self.api(path, port, **kw)
        with self.assertRaises(ObservationUnavailable):
            self.wait(api)

    def test_unexpected_running_is_operational_risk(self):
        self.runtime['payload']['is_running'] = True
        with self.assertRaises(OperationalRisk):
            self.wait()

    def test_programming_error_not_misclassified(self):
        with self.assertRaises(TypeError):
            self.wait(Mock(side_effect=TypeError('defect')))

    def test_probe_timeout_cannot_exceed_remaining_budget(self):
        def api(path, port=8000, timeout=5):
            self.assertLessEqual(timeout, 120-self.now)
            self.sleep(timeout)
            raise TimeoutError()
        with self.assertRaises(ObservationUnavailable):
            self.wait(api)
        self.assertEqual(self.now, 120)


class WrapperTests(unittest.TestCase):
    def test_deploy_rolls_back_only_established_startup_failure(self):
        for error, expected_rollbacks in [
            (EstablishedStartupFailure('persistent failure'), 1),
            (ObservationUnavailable('no supervisor'), 0),
            (OperationalRisk('motion'), 0),
            (TypeError('programming defect'), 0),
        ]:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = {'identity': {'pid': 10}, 'config': {'sha256': 'current'}}
                (root/'resume-preflight.json').write_text(json.dumps({'snapshot': state}))
                replacement = Mock(original={}, prepared={}, applied={'one.py'})
                ns = dict(MODE='deploy', T=root, json=json, snapshot=lambda: state,
                          durable=lambda *args: {}, inventory=lambda *args: {},
                          replacement=replacement, M={}, save=Mock(), now=lambda: 'now',
                          configuration=lambda: state['config'], stopped=Mock(),
                          api=Mock(return_value={'backend_running': False}),
                          start_once=Mock(side_effect=[error, {'pid': 12}]),
                          EstablishedStartupFailure=EstablishedStartupFailure)
                tree = ast.parse(WORKER.read_text())
                tree.body = [n for n in tree.body if isinstance(n, ast.If)
                             and 'MODE' in ast.unparse(n.test)]
                with self.assertRaises(type(error)):
                    exec(compile(tree, '<offline-deploy-control>', 'exec'), ns)
                self.assertEqual(replacement.rollback.call_count, expected_rollbacks)

    def test_config_changes_are_evidence_not_veto(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'software').mkdir()
            p = root/'software/machine.toml'
            ns = functions('configuration', R=root, tomllib=tomllib,
                           sha=lambda raw: hashlib.sha256(raw).hexdigest())
            for model, other in [('hive:one', 1), ('hive:two', 2)]:
                raw = f'other={other}\n[detection.carousel]\nalgorithm="{model}"\n'
                p.write_text(raw)
                self.assertEqual(ns['configuration']()['carousel_algorithm'], model)
                self.assertEqual(p.read_text(), raw)

    def test_start_response_loss_observes_without_repeating_post(self):
        api = Mock(side_effect=TimeoutError('response lost'))
        waiter = Mock(return_value={'pid': 11})
        ns = functions('start_once', save=Mock(), now=lambda: 'now', api=api,
                       TRANSIENT_READ_ERRORS=TRANSIENT_READ_ERRORS,
                       wait_for_startup=waiter, identity=Mock())
        self.assertEqual(ns['start_once']('candidate'), {'pid': 11})
        api.assert_called_once_with('/api/supervisor/start', 8001, True)
        waiter.assert_called_once()

    def test_composed_script_compiles_without_execution(self):
        base = ARTIFACTS/'c3-c4-deployment-20260912/blocker-resolution-20260912'
        script = '\n'.join((base/n).read_text() for n in ('atomic_install.py', 'standby_acceptance.py'))
        script += '\n' + Path(__file__).with_name('deployment_readiness.py').read_text()
        script += '\n' + WORKER.read_text()
        compile(script, '<composed-worker>', 'exec')

    def test_single_old_crash_passes_standby_check(self):
        base = ARTIFACTS/'c3-c4-deployment-20260912'
        ns = {}
        exec((base/'blocker-resolution-20260912/standby_acceptance.py').read_text(), ns)
        state = json.loads((base/'remote-evidence/post-state.json').read_text())
        state['supervisor']['consecutive_fast_crashes'] = 1
        self.assertTrue(all(ns['check'](state, fresh_start=True).values()))
        state['supervisor']['consecutive_fast_crashes'] = 2
        self.assertFalse(ns['check'](state, fresh_start=True)['healthy_supervisor'])


if __name__ == '__main__':
    unittest.main()
