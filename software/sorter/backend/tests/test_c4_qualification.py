import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server.routers import c4_qualification as q
from test_physical_binding import rig


@pytest.mark.parametrize('stopped,stalled', [(False, False), (True, True), (True, False)])
def test_unbound_claim_can_close_only_when_stationary(monkeypatch, stopped, stalled):
    session = q.Session()
    session.claimed = True
    motor = SimpleNamespace(stopped=stopped, stalled=stalled)
    monkeypatch.setattr(session, '_devices', lambda: (motor, SimpleNamespace(stepper=motor)))
    try:
        if stopped and not stalled:
            assert session.close()['unbound'] is True
            assert not session.claimed
        else:
            with pytest.raises(RuntimeError, match='stopped unstalled'):
                session.close()
            assert session.claimed
    finally:
        session.executor.shutdown(wait=True)


def session_for(monkeypatch, rig):
    build, motor, _, _, _ = rig
    session = q.Session()
    session.claimed = True
    session.binding = build()
    session.binding_id = "test-run"
    monkeypatch.setattr(session, '_devices', lambda: (motor, None))
    monkeypatch.setattr(q.time, 'sleep', lambda _: motor.finish())
    return session, motor


def test_one_request_one_index_and_seventh_discharge(monkeypatch, rig):
    session, motor = session_for(monkeypatch, rig)
    session.deposit(q.Deposit(confirmed=True))
    for boundary in range(1, 8):
        receipt = session.advance(q.Advance(binding_id="test-run", expected_boundary=boundary-1, empty_intake=boundary > 1))
        assert receipt['boundary'] == boundary
        assert len(motor.commands) == boundary
        assert len(receipt['discharges']) == (1 if boundary == 7 else 0)
    assert receipt['discharges'][0]['destination'] == 'discard'
    assert receipt['fall_clear_at'] >= receipt['samples'][-1]['monotonic'] + 1.5
    assert all(s['feedback_started_at'] <= s['monotonic'] for s in receipt['samples'])


def test_missing_submission_ack_latches_fault_and_cannot_retry(monkeypatch, rig):
    session, motor = session_for(monkeypatch, rig)
    session.deposit(q.Deposit(confirmed=True))
    motor.accept = False
    with pytest.raises(RuntimeError, match='rejected'):
        session.advance(q.Advance(binding_id="test-run", expected_boundary=0))
    with pytest.raises(RuntimeError, match='rejected'):
        session.advance(q.Advance(binding_id="test-run", expected_boundary=0))
    assert len(motor.commands) == 1


def test_other_controls_excluded_and_emergency_stop_latches(monkeypatch):
    session = q.Session()
    monkeypatch.setattr(q, 'session', session)
    app = FastAPI()
    calls = []
    @app.post('/resume')
    def resume():
        calls.append('resume')
    @app.post('/stepper/stop-all')
    def stop():
        calls.append('stop')
    q.install(app)
    client = TestClient(app)
    assert client.post('/resume').status_code == 200
    session.claimed = True
    assert client.post('/resume').status_code == 409
    assert client.post('/stepper/stop-all').status_code == 200
    assert session.aborted
    assert calls == ['resume', 'stop']


def test_disconnect_keeps_admission_until_worker_finishes(monkeypatch):
    session = q.Session()
    monkeypatch.setattr(q, 'session', session)
    entered, release = threading.Event(), threading.Event()
    def operation():
        entered.set()
        assert release.wait(3)
    async def scenario():
        task = asyncio.create_task(q._run(operation))
        while not entered.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.busy
        with pytest.raises(HTTPException):
            await q._run(lambda: None)
        release.set()
        for _ in range(1000):
            if not session.busy:
                break
            await asyncio.sleep(.001)
        assert not session.busy
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        session.executor.shutdown(wait=True)


def test_claim_rejects_existing_normal_controller(monkeypatch):
    monkeypatch.setattr(q.shared_state, 'controller_ref', SimpleNamespace())
    monkeypatch.setattr(q.shared_state, 'hardware_state', 'initialized')
    with pytest.raises(RuntimeError, match='without creating'):
        q.Session().claim()


def test_completed_request_replays_receipt_without_second_motion(monkeypatch, rig):
    session, motor = session_for(monkeypatch, rig)
    session.deposit(q.Deposit(confirmed=True))
    request = q.Advance(binding_id="test-run", expected_boundary=0)
    first = session.advance(request)
    assert session.advance(request) is first
    assert len(motor.commands) == 1
    with pytest.raises(RuntimeError, match='expected boundary'):
        session.advance(q.Advance(binding_id="test-run", expected_boundary=3, empty_intake=True))


def test_chute_failure_during_active_index_faults(monkeypatch, rig):
    session, motor = session_for(monkeypatch, rig)
    session.deposit(q.Deposit(confirmed=True))
    observe = session.binding.chute.observe
    def observed(now):
        if motor.commands:
            raise RuntimeError('chute stalled')
        return observe(now)
    monkeypatch.setattr(session.binding.chute, 'observe', observed)
    with pytest.raises(RuntimeError, match='chute stalled'):
        session.advance(q.Advance(binding_id="test-run", expected_boundary=0))
    assert session.binding.runtime.lifecycle is q.Lifecycle.FAULTED


def test_chute_calibration_rejection_faults(monkeypatch, rig):
    session, motor = session_for(monkeypatch, rig)
    def rejected(move):
        raise RuntimeError('chute rejected')
    monkeypatch.setattr(session.binding.chute, 'move', rejected)
    with pytest.raises(RuntimeError, match='chute rejected'):
        session.chute_move(q.Destination(destination='A'))
    assert session.binding.runtime.lifecycle is q.Lifecycle.FAULTED
    with pytest.raises(RuntimeError, match='chute rejected'):
        session.advance(q.Advance(binding_id="test-run", expected_boundary=0, empty_intake=True))


def test_old_binding_request_cannot_replay_into_new_binding(monkeypatch, rig):
    session, motor = session_for(monkeypatch, rig)
    session.deposit(q.Deposit(confirmed=True))
    old_request = q.Advance(binding_id='test-run', expected_boundary=0)
    session.advance(old_request)
    session.binding_id = 'new-run'
    session.index_receipts = {}
    with pytest.raises(RuntimeError, match='binding identity'):
        session.advance(old_request)
    assert len(motor.commands) == 1


def test_open_creates_new_receipt_epoch(monkeypatch, rig):
    build, motor, chute, doors, _ = rig
    session = q.Session()
    session.irl = SimpleNamespace(servos=list(doors.values()))
    session.origin_verified = True
    session.origin_position = motor.position
    session.index_receipts[0] = {'kind': 'index_complete', 'boundary': 1}
    session.binding_id = 'previous-run'
    monkeypatch.setattr(session, '_devices', lambda: (motor, chute))
    params = q.OpenBinding(revolution_numerator=32000, revolution_denominator=3,
        clockwise_sign=-1, release_numerator=0, release_denominator=1,
        release_after_start_s=0, arrival_margin_s=.1, door_travel_s=.3)
    receipt = session.open(params)
    assert receipt['binding_id'] != 'previous-run'
    assert session.index_receipts == {}
    assert receipt['fall_clear_by_destination'] == {'DISCARD': pytest.approx(.85)}
