"""Bounded transfer tests using the real ledger, feeder owner and event/accounting path."""
from dataclasses import asdict
from types import SimpleNamespace
import pytest
from defs.known_object import KnownObject, PieceStage
from perception.state import ChannelState, PieceObservation
from runtime_stats import RuntimeStatsCollector
from subsystems.classification_channel.pocket_ledger import PocketRoute
from subsystems.classification_channel.transfer_episode import TransferEpisode
from subsystems.feeder.go_to_angle.config import GoToAngleConfig
from utils.event import knownObjectToEvent
from test_indexed_pocket_pipeline import _pipeline
from test_eject_controller import _owned_feeder

class Evidence:
    """Strict paired C3/C4 frames in a calibrated circular geometry."""
    def __init__(self, clock):
        import numpy as np
        from perception.channel import ChannelDef
        self.clock=clock; self.arrived=False; self.stale=False
        self.leader_id=42; self.trailing_gap=None; self.gap=0.0; self.missing=False
        self.width=4; self.extra=[]; self.c3_stale=False; self.override=None
        self.channel=ChannelDef(3,'c_channel_3',(500,500),0,np.ones((1001,1001),dtype=np.uint8),
                               frozenset(range(160,190)),frozenset(range(340,360))|frozenset(range(25)),
                               frozenset(range(340,360)))
    def piece(self,gap,track,width=None):
        import math
        x=500+400*math.cos(math.radians(-gap));y=500+400*math.sin(math.radians(-gap))
        w=self.width if width is None else width
        return PieceObservation(gap,int(-gap)%360,3 if gap>0 else 2,
                                (int(x-w),int(y-w),int(x+w),int(y+w)),track)
    def channels(self):return {3:self.channel}
    def pieces(self):
        if self.override is not None:return tuple(self.override)
        out=[] if self.missing else [self.piece(self.gap,self.leader_id)]
        if self.trailing_gap is not None:out.append(self.piece(self.trailing_gap,99))
        return tuple(out+self.extra)
    def read_states(self):
        pieces=self.pieces()
        return {3:ChannelState(ts=1000+self.clock[0],n_pieces=len(pieces),in_drop=False,in_exit=bool(pieces),pieces=pieces)}
    def read_pieces_and_frame(self,ch):
        assert ch in (3,4)
        stamp=999.0 if (self.c3_stale if ch==3 else self.stale) else 1000+self.clock[0]
        return (self.pieces() if ch==3 else ([SimpleNamespace(zone_code=1)] if self.arrived else []),
                SimpleNamespace(timestamp=stamp,bgr=None))


def setup_episode(monkeypatch):
    f,clock=_owned_feeder(monkeypatch);clock[0]=100.0
    monkeypatch.setattr('time.time',lambda:1000+clock[0])
    e=Evidence(clock);p=_pipeline(perception_service=e);p.gc.runtime_stats=f.gc.runtime_stats
    f.gc.perception_service=e;f.shared=p.shared;f._cfg=lambda:GoToAngleConfig(ch3_fast_eject_enabled=False)
    p.shared.request_c3_recovery=f._recover_transfer;p.shared.c3_release_leader_id=42;p.shared.release_attempt_mono=100
    from subsystems.feeder.go_to_angle.recovery import capture_support
    # Normal release has already carried this observed piece from +5 to 0.
    # Recovery tests must not assume commanded travel itself proves progress.
    e.gap=5
    evidence=capture_support(e);evidence['anchor']=dict(evidence['material'][0]);evidence['followers']=[]
    evidence['motion_deg']=5
    e.gap=0
    p.shared.c3_release_evidence=evidence
    p._waitIntake(100)
    current=capture_support(e)
    p._episode.support=dict(current['material'][0])
    p._episode.support_motion_deg=0
    def tick(at,arrived=False):
        clock[0]=at;e.arrived=arrived;f._last_perception_tick=at;f._motion_tick+=1;p._waitArrival(at)
    return p,f,clock,e,tick

def finish_move(f,clock):
    stepper=f.irl.c_channel_3_rotor_stepper;stepper.position=stepper.target;clock[0]+=1;f._motion_tick+=1
    assert not f._busy(stepper)

def test_normal_arrival_one_episode_no_reject(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);t(100.1,True);t(100.2,True)
    assert len(p._ledger.loads)==1 and not p._tail.route_locked and p._episode.first_pass
    assert p._tail.payload.ctx.known_object.transport_failure_reason is None
    assert p.gc.runtime_stats.snapshot()['transfer_throughput']['new_transfer_episodes']==1


def test_bounded_staging_overlaps_c4_index_then_releases_only_when_aligned(monkeypatch):
    from perception.cascade import Action
    from subsystems.classification_channel.indexed_pocket_pipeline import _Phase
    from unittest.mock import Mock
    p,f,c,e,t=setup_episode(monkeypatch)
    t(100.1,True);t(100.2,True)
    p._phase=_Phase.INDEX_ONE_POCKET
    p._setGate(False,'capture complete')
    e.secondary_zone_occupied=lambda *a,**k:False
    s=f.irl.c_channel_3_rotor_stepper
    stage=ChannelState(ts=1100.2,in_drop=True,in_exit=False,n_pieces=1,
                       advance_clearance_deg=.75)
    f._on_ch3_release_attempt=Mock()
    f._drive_channel('ch3',3,Action.ADVANCE,stage,None,False,s,f._cfg(),e,c[0])
    assert p.shared.c3_motion_pending and p.shared.c3_safe_staging_pending
    assert s.moves==[pytest.approx(.75*130/12)]  # No minimum-pulse overshoot.
    p._indexOnePocket(c[0])
    assert len(p._stepper.moves)==1 and p._move_target_steps is not None
    assert s._name in f._move_targets  # Indexing does not clear C3 ownership.
    p._stepper.stopped=False
    finish_move(f,c)
    assert not p.shared.c3_safe_staging_pending
    at_exit=ChannelState(ts=1101.2,in_drop=False,in_exit=True,n_pieces=1,
                         exit_com_forward_deg=0,advance_clearance_deg=0)
    for _ in range(3):
        p._indexOnePocket(c[0])
        f._drive_channel('ch3',3,Action.PRECISE,at_exit,None,
                         p.shared.classification_ready,s,f._cfg(),e,c[0])
    assert len(s.moves)==1 and not p.shared.classification_ready
    f._on_ch3_release_attempt.assert_not_called()
    p._stepper.stopped=True
    p._indexOnePocket(c[0]);p._waitIntake(c[0])
    assert p.shared.classification_ready
    f._drive_channel('ch3',3,Action.PRECISE,at_exit,None,True,s,f._cfg(),e,c[0])
    assert len(s.moves)==2 and p.shared.c3_motion_pending
    assert not p.shared.c3_safe_staging_pending
    f._on_ch3_release_attempt.assert_called_once()


@pytest.mark.parametrize('purpose',['precise','followthrough','transport_recovery','advance'])
def test_c4_keeps_waiting_for_owned_nonstaging_c3_motion(monkeypatch,purpose):
    p,f,c,e,t=setup_episode(monkeypatch);t(100.1,True);t(100.2,True)
    s=f.irl.c_channel_3_rotor_stepper
    assert f._move('ch3_'+purpose,s,3,0,f._cfg(),enforce_min=False)
    assert not p.shared.c3_safe_staging_pending
    p._indexOnePocket(c[0]);assert p._stepper.moves==[]
    finish_move(f,c)
    p._indexOnePocket(c[0]);assert len(p._stepper.moves)==1


def test_unresolved_handoff_cannot_be_relabelled_as_safe_staging(monkeypatch):
    from perception.cascade import Action
    p,f,c,e,t=setup_episode(monkeypatch)
    p._episode.state='unresolved'
    e.secondary_zone_occupied=lambda *a,**k:False
    s=f.irl.c_channel_3_rotor_stepper
    stage=ChannelState(ts=1100,in_drop=True,in_exit=False,n_pieces=1,
                       advance_clearance_deg=20)
    f._drive_channel('ch3',3,Action.ADVANCE,stage,None,False,s,f._cfg(),e,c[0])
    p.step()
    assert not s.moves and not p._stepper.moves
    assert p._episode.state=='unresolved'

def test_late_arrival_locks_reject_against_provider_result(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);t(102.9,True);t(103.1,True)
    obj=p._tail.payload.ctx.known_object
    assert obj.transport_failure_reason=='c3_arrival_unconfirmed' and p._episode.followthrough_count==0
    assert p._tail.route_locked and p._tail.route==PocketRoute.REJECT
    p._tail.payload.ctx.classify_started_at=100
    p._tail.payload.ctx.classification_result={'items':[{'id':'3001','score':1}],'colors':[]}
    p._applyResults();assert obj.part_id is None
    p.step();assert len(p._ledger.loads)==1 and len(p.shared.deliveries)==1

def test_one_followthrough_holds_owner_then_rejects_and_drains(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);eid=p._episode.episode_id
    t(101);t(102);t(103.1)
    assert p._episode.followthrough_count==1 and p.shared.c3_motion_pending
    assert f.irl.c_channel_3_rotor_stepper.moves==[pytest.approx(28*130/12)]
    assert len(p._episode.recovery_legs)==1
    finish_move(f,c);t(c[0]+.1,True);t(c[0]+.1,True)
    assert p._episode.episode_id==eid and p._tail.route_locked and p._tail.route==PocketRoute.REJECT
    assert len(p.shared.deliveries)==1
    p._indexOnePocket(c[0]+.1);p._indexOnePocket(c[0]+.2)
    assert p._drain_armed_at>0 and not p.shared.classification_ready

def test_pause_preserves_budget_and_demands_fresh_frames(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);t(101);t(102);t(103.1);eid=p._episode.episode_id
    c[0]=104;p.pause();c[0]=204;p.resume()
    assert p._episode.episode_id==eid and p._episode.followthrough_count==1
    assert p._arrival_armed_at_mono==pytest.approx(203.1)
    assert p._episode.followthrough_at_mono==pytest.approx(103.1)
    e.stale=True;t(204.1,True);assert not p._ledger.loads

def test_real_completion_event_exactly_once_and_not_at_timeout(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);t(102.9,True);t(103.1,True);obj=p._tail.payload.ctx.known_object;s=p.gc.runtime_stats
    s.observeKnownObject(knownObjectToEvent(obj).data.model_dump());assert s.snapshot()['transfer_throughput']['completed_loads']==0
    obj.stage=PieceStage.distributed;obj.distributed_at=1104.0;event=knownObjectToEvent(obj).data.model_dump()
    s.observeKnownObject(event);s.observeKnownObject(event);s.observeTransferEpisode(asdict(p._episode));v=s.snapshot()['transfer_throughput']
    assert v['completed_by_outcome']['transport']==1 and v['completed_by_outcome']['useful']==0
    assert v['cohort_reconciled'] and v['transport_discard_percent']==100

@pytest.mark.parametrize('category',['handling','classification','provider','surplus'])
def test_reject_categories_and_unknown_clump_not_counted_as_one(category):
    s=RuntimeStatsCollector();ep=TransferEpisode(0,0,1,1000);s.observeTransferEpisode(asdict(ep))
    obj=KnownObject(transfer_episode_id=ep.episode_id,stage=PieceStage.distributed,distributed_at=1001,reject_category=category)
    s.observeKnownObject(knownObjectToEvent(obj).data.model_dump());assert s.snapshot()['transfer_throughput']['completed_by_outcome'][category]==1
    ep2=TransferEpisode(1,1,2,1002);s.observeTransferEpisode(asdict(ep2))
    obj2=KnownObject(transfer_episode_id=ep2.episode_id,stage=PieceStage.distributed,distributed_at=1003,physical_group_size_unknown=True,reject_category='handling')
    s.observeKnownObject(knownObjectToEvent(obj2).data.model_dump());v=s.snapshot()['transfer_throughput']
    assert v['total_completed_pieces'] is None and v['transport_discard_percent'] is None and v['unknown_size_completed_groups']==1

def test_useful_completion_and_durable_history_fields():
    from run_recorder import _serializePiece
    s=RuntimeStatsCollector();ep=TransferEpisode(0,0,1,1000);s.observeTransferEpisode(asdict(ep))
    obj=KnownObject(transfer_episode_id=ep.episode_id,part_id='3001',stage=PieceStage.distributed,distributed_at=1001,destination_bin=(0,0,0))
    s.observeKnownObject(knownObjectToEvent(obj).data.model_dump());v=s._transferSnapshot(1060)
    assert v['attempts_per_minute']==v['useful_first_pass_per_minute']==1 and v['transport_discard_percent']==0
    assert _serializePiece(obj)['transfer_episode_id']==ep.episode_id

def test_forced_harvest_excludes_late_candidates_preserves_evidence(monkeypatch):
    import project_harvest_runtime as runtime
    from unittest.mock import create_autospec
    from project_harvest_projects import HarvestProjectStore
    store=create_autospec(HarvestProjectStore,instance=True,spec_set=True);store.has_active_runtime.return_value=True
    store.propose_live_allocation.return_value={'mode':'live','exception':True};monkeypatch.setattr(runtime,'_store',lambda gc:store)
    obj=KnownObject(part_id='3001',color_id='2',forced_reject_reason='transport',classification_item_candidates=[{'id':'3001','score':1}],classification_color_candidates=[{'id':'2','score':1}])
    runtime.reserve_piece(SimpleNamespace(project_harvest_dir='fixture'),obj);args=store.propose_live_allocation.call_args.kwargs
    assert args['part_id'] is None and args['color_id'] is None and args['item_candidates']==args['color_candidates']==args['classification_attempts']==[]
    assert obj.classification_item_candidates==[{'id':'3001','score':1}]
    obj.forced_reject_reason=None;runtime.reserve_piece(SimpleNamespace(project_harvest_dir='fixture'),obj)
    assert store.propose_live_allocation.call_args.kwargs['part_id']=='3001'


def test_outcomes_survive_real_sqlite_upsert_and_legacy_migration(monkeypatch, tmp_path):
    import sqlite3
    import piece_records as records
    from run_recorder import _serializePiece
    db = tmp_path / "history.sqlite"
    # A pre-migration row must remain present with unknown (NULL) new fields.
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE piece_records (id INTEGER PRIMARY KEY, uuid TEXT UNIQUE, run_id TEXT, machine_id TEXT, seen_at REAL, recorded_at REAL, classification_status TEXT, part_id TEXT, part_name TEXT, color_id TEXT, color_name TEXT, category_id TEXT, confidence REAL, bin_x INTEGER, bin_y INTEGER, bin_z INTEGER)")
        conn.execute("INSERT INTO piece_records (uuid) VALUES ('legacy')")
    monkeypatch.setattr(records, "local_state_db_path", lambda: db)
    monkeypatch.setattr(records, "_initialized", False)
    monkeypatch.setattr(records, "_estValue", lambda *args: None)
    obj = KnownObject(transfer_episode_id="episode", transport_failure_reason="c3_arrival_unconfirmed",
                      forced_reject_reason="c3_arrival_unconfirmed", reject_category="transport",
                      physical_group_size_unknown=True, stage=PieceStage.distributed, distributed_at=1001)
    payload = _serializePiece(obj)
    records.recordPiece(payload, run_id="run")
    records.recordPiece(payload, run_id="run")
    stored = records.getPieceSummaryByUuid(None, str(obj.uuid))
    assert stored["transfer_episode_id"] == "episode"
    assert stored["transport_failure_reason"] == stored["forced_reject_reason"] == "c3_arrival_unconfirmed"
    assert stored["physical_group_size_unknown"] == 1 and stored["reject_category"] == "transport"
    assert records.getPieceSummaryByUuid(None, "legacy")["transfer_episode_id"] is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM piece_records").fetchone()[0] == 2


def test_resolved_failure_readmits_at_first_guarded_boundary_once(monkeypatch):
    from subsystems.classification_channel.indexed_pocket_pipeline import _DRAIN_GATE_GUARD_S, _Phase
    p, f, c, e, tick = setup_episode(monkeypatch)
    tick(102.9, True); tick(103.1, True)
    p._indexOnePocket(c[0]); p._indexOnePocket(c[0])
    assert p._ledger.boundary_index == 1 and p._tail is None
    guard = p._drain_armed_at
    e.arrived = False
    for delta in [0.1, 0.2, _DRAIN_GATE_GUARD_S + 0.01]:
        c[0] = guard + delta
        p._waitIntake(c[0])
        assert p._drain_armed_at == guard
    p._indexOnePocket(c[0])
    assert p._phase == _Phase.WAIT_INTAKE and p.shared.classification_ready and p._readmission_used
    assert p._ledger.boundary_index == 1 and len(p._ledger.loads) == 1
    c[0] = p._intake_deadline + .01
    p._waitIntake(c[0]); c[0] += _DRAIN_GATE_GUARD_S + .01; p._waitIntake(c[0])
    for _ in range(2): p._indexOnePocket(c[0])
    assert p._ledger.boundary_index == 2 and not p.shared.classification_ready


def test_post_release_but_stale_frames_cannot_admit(monkeypatch):
    p, f, c, e, tick = setup_episode(monkeypatch)
    e.read_pieces_and_frame = lambda ch: ([SimpleNamespace(zone_code=1)], SimpleNamespace(timestamp=1100.1, bgr=None))
    tick(102)
    e.read_pieces_and_frame = lambda ch: ([SimpleNamespace(zone_code=1)], SimpleNamespace(timestamp=1100.2, bgr=None))
    tick(102.1)
    assert not p._ledger.loads and p._arrival_presence_streak == 0


def test_real_detector_request_reports_outage_streak_without_piece_discard():
    import threading
    import numpy as np
    from vision.vision_manager import VisionManager
    from vision.detection_registry import DetectionRequest
    class Detector:
        _last_error = "HTTP 402 credit exhausted"
        def detect(self, crop, force):
            return None if self._last_error else SimpleNamespace()
    detector = Detector()
    manager = VisionManager.__new__(VisionManager)
    stats = RuntimeStatsCollector()
    manager.gc = SimpleNamespace(runtime_stats=stats)
    manager._geminiDetectorForRequest = lambda request: detector
    manager._openrouter_request_lock = threading.Lock()
    manager._openrouter_semaphore = threading.BoundedSemaphore(1)
    request = DetectionRequest(scope="classification", role="classification_channel", frame=np.zeros((2, 2, 3)), force=True)
    for error in ["HTTP 402", "HTTP 402", None, "HTTP 402"]:
        detector._last_error = error
        manager._openrouter_next_allowed_at = 0.0
        manager._runGeminiDetectionRequestWithThrottle(request)
    result = stats.snapshot()["transfer_throughput"]
    assert result["provider_outages"] == 2
    assert result["provider_availability"]["openrouter_detector"]["failure_observations"] == 3
    assert result["completed_by_outcome"]["transport"] == result["completed_loads"] == 0


def test_unexpected_second_release_preserves_one_episode_and_unknown_group(monkeypatch):
    p, f, c, e, tick = setup_episode(monkeypatch)
    eid = p._episode.episode_id
    p.shared.release_attempt_mono = 100.05
    tick(100.1)
    assert p._episode.state == "unresolved" and p._episode.group_size_unknown
    tick(100.2, True); tick(100.3, True)
    assert p._episode.episode_id == eid and p._episode.group_size_unknown
    assert p._episode.state == 'unresolved' and p._tail is None
    assert not p._ledger.loads and not p.shared.deliveries
    assert p._deps[-1].qsize() == 1
    assert p.gc.runtime_stats.snapshot()["transfer_throughput"]["new_transfer_episodes"] == 1


def test_unresolved_c2_owner_prevents_followthrough(monkeypatch):
    p, f, c, e, tick = setup_episode(monkeypatch)
    upstream = f.irl.c_channel_2_rotor_stepper
    f._move_targets[upstream._name] = 123456
    tick(101); tick(102); tick(103.1)
    assert p._episode.recovery_decision['result'] == 'wait'
    assert not f.irl.c_channel_3_rotor_stepper.moves
    tick(115.2)
    assert p._episode.state == 'unresolved'


def test_real_positioning_cannot_promote_reject_to_high_value_bin(monkeypatch):
    import queue
    from unittest.mock import Mock
    from subsystems.distribution.positioning import Positioning
    from subsystems.distribution.states import DistributionState
    from irl.bin_layout import DistributionLayout, Layer, BinSection, Bin, BinSize
    from sorting_profile import MISC_CATEGORY
    obj = KnownObject(part_id="3001", color_id="2", moving_avg_price=99,
                      forced_reject_reason="c3_arrival_unconfirmed", high_value_routed=True)
    gc = SimpleNamespace(logger=Mock(), runtime_stats=RuntimeStatsCollector(), project_harvest_dir=None)
    shared = SimpleNamespace(transport=SimpleNamespace(getPieceForDistributionPositioning=lambda: obj),
                             set_distribution_gate=Mock())
    layout = DistributionLayout(layers=[Layer(sections=[BinSection(bins=[Bin(BinSize.SMALL, category_ids=["valuable"])])])])
    profile = SimpleNamespace(highValueCategoryId=lambda price: "valuable")
    chute = SimpleNamespace(isBinReachable=lambda address: True)
    state = Positioning(SimpleNamespace(), gc, shared, chute, layout, profile, queue.Queue())
    monkeypatch.setattr("local_state.get_current_bin_piece_counts", lambda: {})
    # Hardware leaf operations are fixtures; real selection and bin lookup run.
    state._isLayerUsable = lambda *args: True
    state._openAllDoorsForPassthrough = Mock()
    state._raiseBinsFullAlert = Mock()
    assert state.step() == DistributionState.READY
    assert obj.category_id == MISC_CATEGORY and obj.part_id is None
    assert not obj.high_value_routed and obj.destination_bin is None
    state._openAllDoorsForPassthrough.assert_called_once()


# Production recovery regressions: concrete paired frames and retained owner.
def seed_evidence(p, e, motion=0):
    from subsystems.feeder.go_to_angle.recovery import capture_support
    snap=capture_support(e);snap['anchor']=dict(snap['material'][0]);snap['followers']=snap['material'][1:]
    snap['motion_deg']=motion;p._episode.release_evidence=snap;p._episode.support={}
    p._episode.support_motion_deg=motion


def start_recovery(tick):
    tick(101);tick(102);tick(103.1)


def test_recorded_floor_stall_crosses_exit_in_one_bounded_move(monkeypatch):
    """Replay a5ff2f38 geometry; boundary crossing is a model, not live proof."""
    from dataclasses import replace
    p,f,c,e,t=setup_episode(monkeypatch)
    e.channel=replace(e.channel,center=(645,356),radius1_angle_image=-24.075518,
                      exit_sections=frozenset(range(25,80)),precise_sections=frozenset(range(25,40)))
    e.override=[
        PieceObservation(-4.835281065420645,44,2,(858,427,986,495),22),
        PieceObservation(7.124720453019819,32,3,(858,308,994,491),29),
        PieceObservation(43.69111501651787,356,0,(891,144,984,260),3),
        PieceObservation(62.16857855287765,337,0,(853,81,910,137),9),
        PieceObservation(83.34492918089484,316,0,(735,19,807,87),6)]
    # Preserve the already-established envelope from the captured decision.
    # This does not change production association or infer a piece count.
    p._episode.support={'low':-18.203247941094276,'high':69.82196590153319,'ids':[22,29,3,9],
                        'radius_low':206.46930111610934,'radius_high':406.80923124910623}
    p._episode.group_size_unknown=True
    eid=p._episode.episode_id
    start_recovery(t)
    leg=p._episode.recovery_legs[0]
    assert leg['degrees']==pytest.approx(38.164718934579355)
    assert p._episode.recovery_decision['current_ids']==[22,29,3,9]
    assert p._episode.recovery_decision['predicates']['forward_sweep_clear']
    # Recorded 3 x 144 microsteps never reach the calibrated far boundary.
    past_entry=4.835281065420645
    old_actual_travel=3*144*360/1600/(130/12)
    assert past_entry+old_actual_travel < 40
    # Use commanded motor travel to drive the synthetic fall model.
    output_travel=f.irl.c_channel_3_rotor_stepper.moves[0]/(130/12)
    crossed=past_entry+output_travel >= 40
    assert crossed and past_entry+output_travel==pytest.approx(43)
    t(103.2,crossed);t(103.3,crossed)
    assert not p._ledger.loads and not p._stepper.moves
    assert p.shared.c3_motion_pending
    finish_move(f,c)
    t(c[0]+.1,crossed);t(c[0]+.1,crossed)
    assert p._episode.episode_id==eid and p._episode.state=='admitted'
    assert len(p._ledger.loads)==len(p.shared.deliveries)==1
    assert p._tail.route_locked and p._tail.route==PocketRoute.REJECT
    assert len(f.irl.c_channel_3_rotor_stepper.moves)==1


@pytest.mark.parametrize('span,gap,maximum',[(float('nan'),0,120),(0,0,120),
                                            (180,0,120),(40,-44,120)])
def test_clearance_endpoint_never_truncates_or_repeats_beyond_exit(span,gap,maximum):
    from subsystems.feeder.go_to_angle.recovery import followthrough_to_exit_end
    assert followthrough_to_exit_end({'exit_span_deg':span},{'material':[{'com':gap}]},
                                    margin=3,maximum=maximum) is None


def test_missing_action_frame_waits_with_same_budget_then_recovers(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch)
    original=e.read_pieces_and_frame
    lost=[False]
    def action_race(episode,boundary,*,action=None):
        if action is not None and not lost[0]:
            lost[0]=True
            e.read_pieces_and_frame=lambda ch:None if ch==3 else original(ch)
            try:return f._recover_transfer(episode,boundary,action=action)
            finally:e.read_pieces_and_frame=original
        return f._recover_transfer(episode,boundary,action=action)
    p.shared.request_c3_recovery=action_race
    start_recovery(t)
    ep=p._episode;deadline=ep.recovery_deadline_mono
    assert lost[0] and ep.state=='recovering' and ep.recovery_decision['result']=='wait'
    assert 'followthrough_endpoint_available' in ep.recovery_decision['blocking_predicates']
    assert not ep.recovery_legs and not f.irl.c_channel_3_rotor_stepper.moves
    t(103.3)
    assert ep.recovery_deadline_mono==deadline and len(ep.recovery_legs)==1
    finish_move(f,c);t(c[0]+.1,True);t(c[0]+.1,True)
    assert ep.state=='admitted' and len(p._ledger.loads)==len(p.shared.deliveries)==1


@pytest.mark.parametrize('gap',[3.7615502,4.3585577])
def test_captured_position_outside_old_com_gate_allows_known_location_forward(monkeypatch,gap):
    from dataclasses import replace
    p,f,c,e,t=setup_episode(monkeypatch)
    e.channel=replace(e.channel,center=(645,356),radius1_angle_image=-24,
                      exit_sections=frozenset(range(25,80)),precise_sections=frozenset(range(25,40)))
    e.override=[PieceObservation(16.5830449,23,0,(977,322,1009,382),48),
                PieceObservation(25.9614042,14,0,(965,267,1003,325),61)]
    p._episode.leader_id=48;seed_evidence(p,e,19.6)
    captured_box=(917,379,1004,469) if gap<4 else (929,381,1006,463)
    follower_gap=18.4747834 if gap<4 else 18.39262
    e.override=[PieceObservation(gap,36,3,captured_box,48),
                PieceObservation(follower_gap,21,0,(975,312,1011,369),67)]
    start_recovery(t)
    assert p._episode.recovery_legs[0]['degrees']==pytest.approx(gap+40+3)
    assert p._episode.group_size_unknown
    assert p._episode.recovery_decision['predicates']['c3_supported_region']
    assert 'leader_com_within_3_degrees' not in p._episode.recovery_decision['predicates']


def test_id_change_uses_spatial_continuity_without_new_episode(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);eid=p._episode.episode_id
    t(101);e.leader_id=77;t(102);t(103.1)
    assert p._episode.followthrough_count==1 and p._episode.episode_id==eid
    assert p._episode.recovery_decision['current_ids']==[77]
    finish_move(f,c);t(104.2);t(104.3);e.leader_id=88;t(106.2)
    assert p._episode.state=='recovering' and len(p._episode.recovery_legs)==1
    t(115.2)
    assert p._episode.state=='discard_bound' and len(p._episode.recovery_legs)==1


def test_one_exit_clearance_attempt_is_bounded_without_blind_retries(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);start_recovery(t)
    assert [l['degrees'] for l in p._episode.recovery_legs]==[28]
    assert p._episode.recovery_branch=='exit_clearance'
    finish_move(f,c);t(106.2);t(106.3)
    assert p._episode.state=='recovering' and len(p._episode.recovery_legs)==2
    assert p._episode.recovery_legs[-1]['kind']=='jitter'
    t(115.2)
    assert p._episode.state=='unresolved' and p._deps[-1].qsize()==1
    for at in [114,120,130]:e.leader_id+=1;t(at)
    assert len(f.irl.c_channel_3_rotor_stepper.moves)==1 and p._deps[-1].qsize()==1


@pytest.mark.parametrize('group',['close','overlap','enlarged'])
def test_full_clearance_sweep_respects_grouping_and_detached_followers(monkeypatch,group):
    p,f,c,e,t=setup_episode(monkeypatch)
    if group=='close':e.trailing_gap=5
    if group=='overlap':e.extra=[e.piece(.5,61)]
    if group=='enlarged':e.width=12
    start_recovery(t)
    if group=='close':
        leg=p._episode.recovery_legs[0]
        assert 0 < leg['degrees'] < p._episode.recovery_decision['forward_clearance_deg'] < 5
        assert p._episode.recovery_decision['predicates']['forward_sweep_clear']
    else:
        assert [l['degrees'] for l in p._episode.recovery_legs]==[28]
        assert p._episode.group_size_unknown
    assert p._episode.recovery_branch=='exit_clearance'


def test_unknown_group_remains_unknown_transport_reject_through_completion(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);e.extra=[e.piece(.5,61)]
    start_recovery(t);finish_move(f,c);t(104.2,True);t(104.3,True)
    obj=p._tail.payload.ctx.known_object
    assert obj.physical_group_size_unknown and p._tail.route_locked and obj.reject_category=='transport'
    obj.stage=PieceStage.distributed;obj.distributed_at=1105
    for _ in range(2):p.gc.runtime_stats.observeKnownObject(knownObjectToEvent(obj).data.model_dump())
    v=p.gc.runtime_stats.snapshot()['transfer_throughput']
    assert v['completed_loads']==v['unknown_size_completed_groups']==1
    assert v['completed_single_pieces']==0 and v['total_completed_pieces'] is None
    assert v['transport_discard_percent'] is None and v['completed_by_outcome']['transport']==0
    assert v['recovered_transport_reject_loads']==1
    assert v['recovery_motor_moves']==1 and v['recovery_motor_moves_per_arrived_load']==1


def test_arrival_during_clearance_waits_for_owned_completion_and_admits_once(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);start_recovery(t)
    n=len(p._episode.recovery_legs);now=c[0]
    # Arrival is remembered but cannot let C4 index before C3's accepted leg ends.
    t(now+.1,True);t(now+.2,True)
    assert not p._ledger.loads
    finish_move(f,c);t(c[0]+.1,True);t(c[0]+.1,True)
    assert len(p._ledger.loads)==1 and len(p.shared.deliveries)==1
    assert p._tail.route_locked and p._tail.route==PocketRoute.REJECT
    assert len(f.irl.c_channel_3_rotor_stepper.moves)==n
    assert p._episode.recovery_success_stage==p._episode.recovery_legs[-1]['stage']


def test_signed_recovery_uses_real_motor_conversion(monkeypatch):
    from hardware.sorter_interface import StepperMotor
    from test_eject_controller import _OwnedStepper
    class Stepper(_OwnedStepper):
        _steps_per_revolution=200
        _microsteps=8
        microsteps_for_degrees=StepperMotor.microsteps_for_degrees
    p,f,c,e,t=setup_episode(monkeypatch);s=Stepper('c3');f.irl.c_channel_3_rotor_stepper=s
    start_recovery(t)
    assert [s.microsteps_for_degrees(d) for d in s.moves]==[1348]
    with pytest.raises(ValueError):f._move('bad',f.irl.c_channel_2_rotor_stepper,1.5,300,f._cfg(),c3_recovery_direction=-1)


@pytest.mark.parametrize('problem',['missing','detached','c4_conflict','misaligned','stale','suppressed','second_transfer','sweep'])
def test_unknown_location_or_unsafe_boundary_cannot_agitate(monkeypatch,problem):
    p,f,c,e,t=setup_episode(monkeypatch)
    if problem=='missing':e.missing=True
    if problem=='detached':e.gap=18
    if problem=='misaligned':p._stepper.position=2
    if problem=='stale':e.c3_stale=True
    if problem=='suppressed':f.irl.c_channel_3_rotor_stepper.software_disabled=True
    if problem=='second_transfer':p.shared.release_attempt_mono=100.01
    if problem=='sweep':
        # Detached follower already at the exit; no positive safe travel.
        e.gap=-15
        p._episode.support={**p._episode.support, 'low':-18,'high':-12}
        e.trailing_gap=0
    if problem=='c4_conflict':p._ledger.boundary_index=1
    start_recovery(t)
    if problem in ('stale','missing','detached'):
        assert p._episode.state=='recovering' and not f.irl.c_channel_3_rotor_stepper.moves
        t(115.2)
    assert not f.irl.c_channel_3_rotor_stepper.moves
    if problem == 'sweep':
        assert p._episode.state=='recovering'
        t(103.2)  # Fresh confirmation of the same no-motion envelope.
    assert p._episode.state == ('discard_bound' if problem == 'missing' else 'recovering' if problem in ('stale','detached') else 'unresolved')
    from defs.events import PauseCommandEvent
    assert sum(isinstance(event, PauseCommandEvent) for event in p._deps[-1].queue) == int(problem not in ('missing','stale','detached'))


def test_lost_ack_spends_leg_preserves_owner_and_never_repeats(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);s=f.irl.c_channel_3_rotor_stepper
    def lose_ack(deg):
        s.moves.append(deg);s.target=s.position+s.microsteps_for_degrees(deg)
        raise OSError('lost acknowledgement after motor accepted')
    s.move_degrees=lose_ack
    start_recovery(t)
    assert p._episode.state=='unresolved' and s._name in f._move_targets
    assert p._episode.recovery_legs[0]['accepted'] is None
    for at in [104,110,120]:t(at)
    assert len(s.moves)==1 and len(p._episode.recovery_legs)==1


def test_pause_does_not_reset_stage_leg_or_active_ceiling(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);start_recovery(t)
    deadline=p._episode.recovery_deadline_mono;ids=[l['key'] for l in p._episode.recovery_legs]
    c[0]=106.3;p.pause();c[0]=206.3;p.resume()
    assert p._episode.recovery_deadline_mono==pytest.approx(deadline+100)
    assert [l['key'] for l in p._episode.recovery_legs]==ids
    finish_move(f,c);t(c[0]+.1)
    assert [l['key'] for l in p._episode.recovery_legs]==['1.forward']
    assert len(f.irl.c_channel_3_rotor_stepper.moves)==1


def test_decision_reports_all_failures_and_does_not_veto_forward_for_reverse_risk(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);e.extra=[e.piece(.5,61)]
    start_recovery(t);d=p._episode.recovery_decision
    assert d['result']=='accepted' and d['predicates']['reverse_sweep_clear'] is False
    assert 'reverse_sweep_clear' in d['failed_predicates']
    assert all(k in d for k in ['material','followers','frame_age_s','boundary','motors','signed_output_degrees','command'])


def test_explicit_rejection_is_terminal_without_repeated_commands(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);s=f.irl.c_channel_3_rotor_stepper;s.accept=False
    start_recovery(t);t(110)
    assert p._episode.state=='unresolved' and len(s.moves)==1
    assert p._episode.recovery_legs[0]['accepted'] is False



def test_confirmed_arrival_stays_cancelled_when_detector_flickers(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);start_recovery(t)
    t(103.2,True);t(103.3,True)
    assert p._episode.recovery_arrival and not p._ledger.loads
    t(103.35,False)
    assert len(p._episode.recovery_legs)==1
    finish_move(f,c);t(c[0]+.1,False)
    assert len(p._ledger.loads)==1 and len(p._episode.recovery_legs)==1
    assert p._tail.route_locked


def test_confirmed_arrival_unfinished_motor_reaches_one_terminal_ceiling(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);start_recovery(t)
    ep=p._episode;s=f.irl.c_channel_3_rotor_stepper;deadline=ep.recovery_deadline_mono
    t(103.2,True);t(103.3,True)
    assert ep.recovery_arrival and ep.state=='recovering'
    for at in [104,108,deadline-.01]:
        t(at,True)
        assert ep.state=='recovering' and p._deps[-1].qsize()==0
    for at in [deadline,deadline+.1,deadline+5]:
        t(at,True)
        assert ep.state=='unresolved' and p._deps[-1].qsize()==1
        assert ep.recovery_decision['blocking_predicates']==['owned_c3_motion_unresolved']
    assert s._name in f._move_targets and p.shared.c3_motion_pending
    assert ep.recovery_deadline_mono==deadline and len(ep.recovery_legs)==len(s.moves)==1
    assert not p._ledger.loads and not p.shared.deliveries
    assert p.gc.runtime_stats.activeIncident()['episode_id']==ep.episode_id


@pytest.mark.parametrize('conflict',['reservation','alignment','suppressed','owner'])
def test_confirmed_arrival_enforces_conflicts_before_motor_completion(monkeypatch,conflict):
    p,f,c,e,t=setup_episode(monkeypatch);start_recovery(t)
    ep=p._episode;s=f.irl.c_channel_3_rotor_stepper
    t(103.2,True);t(103.3,True)
    if conflict=='reservation':p._ledger.boundary_index+=1
    if conflict=='alignment':p._stepper.position+=2
    if conflict=='suppressed':s.software_disabled=True
    if conflict=='owner':p.shared.c3_motion_pending=False
    for at in [103.4,103.5]:t(at,True)
    expected={'reservation':'reserved_boundary','alignment':'c4_stopped_aligned',
              'suppressed':'motors_unsuppressed','owner':'c3_owner_consistent'}[conflict]
    assert expected in ep.recovery_decision['blocking_predicates']
    assert ep.state=='unresolved' and p._deps[-1].qsize()==1
    assert s._name in f._move_targets and len(s.moves)==len(ep.recovery_legs)==1
    assert ep.recovery_arrival and not p._ledger.loads and not p.shared.deliveries


def test_clearance_first_arrival_frame_waits_for_confirmation_not_another_leg(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);start_recovery(t)
    finish_move(f,c);e.missing=True;t(c[0]+.1,True)
    assert p._episode.state=='recovering' and len(p._episode.recovery_legs)==1
    t(c[0]+.1,True)
    assert len(p._ledger.loads)==1 and len(p._episode.recovery_legs)==1


def test_radially_unrelated_material_cannot_replace_active_load(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch)
    e.override=[PieceObservation(0,0,2,(600,496,608,504),42)]
    start_recovery(t)
    assert p._episode.state=='recovering' and not f.irl.c_channel_3_rotor_stepper.moves
    assert not p._episode.recovery_decision['predicates']['spatial_association']
    t(115.2)
    assert p._episode.state=='discard_bound' and not f.irl.c_channel_3_rotor_stepper.moves
    assert p._tail.route == PocketRoute.REJECT
    assert p._tail.pocket_id == p._episode.pocket_id
    assert p.gc.runtime_stats.activeIncident() is None


def test_motion_must_fit_remaining_active_budget(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);t(101);t(102)
    p._episode.recovery_started_mono=100;p._episode.recovery_deadline_mono=103.2
    p._episode.state='recovering'
    t(103.1)
    assert p._episode.state=='recovering' and not f.irl.c_channel_3_rotor_stepper.moves
    assert not p._episode.recovery_decision['predicates']['leg_within_time_budget']
    assert not p.gc.runtime_stats.activeIncident()
    t(103.3)
    assert p._episode.state=='recovering'  # Positive identity permits bounded physical continuation.
    assert p._episode.recovery_legs[-1]['retained']
    assert p._episode.recovery_deadline_mono==103.2


def test_owner_refuses_second_recovery_command_even_with_new_grouping(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);start_recovery(t);finish_move(f,c)
    t(104.2);t(104.3)
    ep=p._episode;e.extra=[e.piece(.5,61)]
    c[0]=106.2;f._last_perception_tick=c[0];f._motion_tick+=1
    boundary=p._recoveryBoundary(1106.2)
    result=f._recover_transfer(ep,boundary,action={'stage':2,'key':'2.reverse','degrees':-1.5})
    assert result['result']=='unsafe' and len(ep.recovery_legs)==2
    assert ep.recovery_legs[-1]['kind']=='jitter'
    assert 'continuation_sequence' in result['blocking_predicates']
    assert ep.group_size_unknown


def test_normal_spatial_observation_does_not_query_motors(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch)
    def unexpected_probe(*args):
        pytest.fail('normal observation added motor I/O')
    monkeypatch.setattr(f, '_busy', unexpected_probe)
    monkeypatch.setattr(p, '_recoveryBoundary', unexpected_probe)
    t(101);t(102)
    assert p._episode.recovery_started_mono is None
    assert all(m['source']=='existing_owner_bookkeeping'
               for m in p._episode.recovery_decision['motors'].values())


def test_reservation_conflict_during_observation_pauses_without_waiting(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch);start_recovery(t)
    p._ledger.boundary_index=1;t(103.2)
    assert p._episode.state=='unresolved' and p._deps[-1].qsize()==1
    assert len(f.irl.c_channel_3_rotor_stepper.moves)==1
    assert 'reserved_boundary' in p._episode.recovery_decision['blocking_predicates']


@pytest.mark.parametrize('preparation_s',[11.9,13])
def test_slow_hardware_preparation_cannot_dispatch_past_deadline(monkeypatch,preparation_s):
    p,f,c,e,t=setup_episode(monkeypatch)
    s=f.irl.c_channel_3_rotor_stepper
    original=s.set_speed_limits
    def slow_preparation(*args):
        original(*args);c[0]+=preparation_s
    s.set_speed_limits=slow_preparation
    start_recovery(t)
    assert not s.moves and s._name not in f._move_targets
    assert p._episode.state=='recovering'
    assert p._episode.recovery_decision['blocking_predicates']==['leg_within_time_budget']
    assert not p.gc.runtime_stats.activeIncident()
    deadline=p._episode.recovery_deadline_mono
    if c[0]<deadline:
        t(c[0]+.01)
        assert p._episode.recovery_open and not p.gc.runtime_stats.activeIncident()
        assert not s.moves and len(p._episode.recovery_legs)==1
    e.missing=True;t(max(c[0]+.1,deadline))
    assert p._episode.state=='discard_bound' and len(p._ledger.loads)==1
    assert p._episode.recovery_deadline_mono==deadline and not s.moves
    assert p._episode.recovery_legs[0]['dispatch_refused']=='leg_within_time_budget'


def test_late_hardware_rejection_is_not_a_pre_dispatch_budget_refusal(monkeypatch):
    p,f,c,e,t=setup_episode(monkeypatch)
    s=f.irl.c_channel_3_rotor_stepper;s.accept=False
    original=s.move_degrees
    def late_rejection(*args):
        result=original(*args);c[0]+=13;return result
    s.move_degrees=late_rejection
    start_recovery(t)
    ep=p._episode
    assert ep.state=='unresolved' and len(s.moves)==1
    assert ep.recovery_legs[0]['accepted'] is False
    assert 'dispatch_refused' not in ep.recovery_legs[0]
    assert p.gc.runtime_stats.activeIncident()['awaiting_operator']
    assert not p._ledger.loads


def test_landed_load_waits_for_delayed_c4_confirmation_without_c3_support(monkeypatch):
    # Physical run a3e2779e: release 0.0, completion 1.17s, absent C3,
    # latest C4 frame still empty at 3.36s, C2 owner not settled yet.
    p,f,c,e,t=setup_episode(monkeypatch)
    t(100.5)
    e.missing=True
    t(101.174)
    upstream=f.irl.c_channel_2_rotor_stepper
    f._move_targets[upstream._name]=123456
    t(103.36)
    ep=p._episode
    assert ep.state=='recovering' and not ep.recovery_legs
    assert not p._deps[-1].qsize() and not p._ledger.loads
    # A single frame or the repeated same frame is insufficient.
    t(103.7,True);t(103.7,True)
    assert not p._ledger.loads
    t(104.0,True)
    assert len(p._ledger.loads)==1 and len(p.shared.deliveries)==1
    assert ep.state=='admitted' and p._tail.route_locked
    assert p._tail.route==PocketRoute.REJECT and ep.forced_reject_reason=='c3_arrival_unconfirmed'
    assert not f.irl.c_channel_3_rotor_stepper.moves
    t(104.1,True)
    assert len(p._ledger.loads)==len(p.shared.deliveries)==1


@pytest.mark.parametrize('observation',['empty','stale','one_frame'])
def test_missing_c3_arrival_observation_is_bounded_and_never_invents_arrival(monkeypatch,observation):
    p,f,c,e,t=setup_episode(monkeypatch);e.missing=True
    t(101);t(102);t(103.1)
    assert p._episode.state=='recovering'
    if observation=='stale':e.stale=True
    t(104,observation!='empty')
    t(104.2,observation=='stale')
    assert not p._ledger.loads
    t(115.2)
    expected = 'recovering' if observation == 'stale' else 'discard_bound'
    assert p._episode.state == expected
    # Fresh disappearance reserves one explicitly unverified reject slot.
    # A stale detector cannot establish the narrow lost-confirmation case.
    assert len(p._ledger.loads) == int(observation != 'stale')
    assert not f.irl.c_channel_3_rotor_stepper.moves
    # Restored fresh evidence can resolve the original slot, never duplicate it.
    e.stale=False;t(116,True);t(116.2,True)
    assert len(p._ledger.loads) == 1
    assert not p.shared.deliveries
    from defs.events import PauseCommandEvent
    assert sum(isinstance(event, PauseCommandEvent) for event in p._deps[-1].queue) == 0
