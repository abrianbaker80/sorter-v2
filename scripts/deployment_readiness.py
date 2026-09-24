"""Bounded, read-only startup observation; never starts, stops or rolls back."""
import json
import time
import urllib.error


class EstablishedStartupFailure(RuntimeError):
    """Repeated crashes or persistent unreadiness after the grace period."""


class ObservationUnavailable(RuntimeError):
    """Command ownership cannot be established; do not infer rollback permission."""


class OperationalRisk(RuntimeError):
    """Observed activity during an inert replacement; requires diagnosis."""


TRANSIENT_READ_ERRORS = (
    urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError,
    FileNotFoundError, ProcessLookupError,
)


def wait_for_startup(api, identity, *, timeout=120, interval=2,
                     clock=time.monotonic, sleep=time.sleep, record=lambda value: None):
    """Require three healthy observations of one process within a wall-clock bound.

    api is read-only here and must honor its timeout argument. A prior isolated
    crash is not a crash loop. Transport errors are observations, not rollback.
    """
    deadline = clock() + timeout
    first = None
    changes = stable = failures = 0
    last_owned = float('-inf')
    last = 'startup not observed'
    while clock() < deadline:
        owned_observation = False
        def read(path, port=8000):
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError('startup observation deadline')
            return api(path, port, timeout=min(5, remaining))

        try:
            sup = read('/api/supervisor/status', 8001)
            if sup.get('crash_looping') or sup.get('consecutive_fast_crashes', 0) >= 2:
                raise EstablishedStartupFailure('repeated backend crashes reported')
            pid = sup.get('backend_pid')
            if not pid:
                stable = failures = 0
                last = 'supervisor has no backend process'
            else:
                current = identity(pid)
                last_owned = clock()
                owned_observation = True
                if current != first:
                    changes += int(first is not None)
                    first, stable, failures = current, 0, 0
                    record(current)
                if changes >= 2:
                    raise EstablishedStartupFailure('backend restarted repeatedly during startup')
                rt = read('/runtime-stats').get('payload', {})
                if rt.get('is_running') is True:
                    raise OperationalRisk('unexpected running activity during inert startup')
                healthy = (sup.get('backend_running') is True
                           and sup.get('backend_healthy') is True
                           and not sup.get('restart_requested')
                           and rt.get('is_running') is False
                           and 'transfer_throughput' in rt
                           and read('/health') == {'status': 'ok'})
                stable = stable + 1 if healthy else 0
                failures = 0 if healthy else failures + 1
                last = 'backend telemetry/health not ready'
                if stable >= 3:
                    return first
        except TRANSIENT_READ_ERRORS as exc:
            stable = 0
            failures = failures + 1 if owned_observation else 0
            last = repr(exc)
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(interval, remaining))
    if clock() - last_owned <= 10 and failures >= 3:
        raise EstablishedStartupFailure(f'backend unready after {timeout}s: {last}')
    raise ObservationUnavailable(f'startup observation incomplete after {timeout}s: {last}')
