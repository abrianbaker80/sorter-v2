"""Existing observed-progress/jitter policy; its host owns C4 reservation.

Shared by the legacy inactive pipeline and the physical FIFO handoff adapter.
This helper only asks the existing C3 feeder owner for observed safe recovery.
"""
import time
from defs.known_object import UNVERIFIED_C4_HANDOFF


class C3TransferRecovery:
    def _advanceRecovery(self, now: float, boundary: dict, observed: dict) -> None:
        ep = self._episode
        if ep.recovery_started_mono is None:
            ep.recovery_started_mono = now
            ep.recovery_deadline_mono = now + 12.0
            ep.state = "recovering"
        ep.recovery_elapsed_s = max(0.0, now-ep.recovery_started_mono)
        # Completion belongs to the command even if its material vanished.
        # Keep the post-motion observation boundary available to either outcome.
        motion_legs = [leg for leg in ep.current_recovery_legs
                       if leg.get('dispatch_refused') != 'leg_within_time_budget']
        last = motion_legs[-1] if motion_legs else None
        if (last and last['accepted'] is True
                and observed['predicates']['motor_c3_resolved']
                and last['completed_at_mono'] is None):
            last['completed_at_mono'] = now
            last['completed_at_wall'] = time.time()
        conflicts = [k for k in ('reserved_boundary', 'c4_available', 'c4_stopped_aligned',
                                 'active_episode', 'c3_enabled', 'motors_unsuppressed', 'c3_owner_consistent')
                     if not observed['predicates'][k]]
        if conflicts:
            ep.recovery_decision['blocking_predicates'] = conflicts
            self._unresolvedTransfer('recovery ownership conflict: ' + ', '.join(conflicts))
            self._recordRecoveryDecision()
            return
        if (now >= ep.recovery_deadline_mono
                and not observed['predicates'].get('motor_c3_resolved')):
            if (last and last.get('retained') and last.get('accepted') is True
                    and now <= last.get('completion_due_mono',0)):
                ep.recovery_decision['result'] = 'wait_for_owned_completion'
                self._recordRecoveryDecision()
                return
            ep.recovery_decision['blocking_predicates'] = ['owned_c3_motion_unresolved']
            self._unresolvedTransfer('C3 motor has not completed its owned recovery command')
            self._recordRecoveryDecision()
            return
        # Confirmation is physical evidence even on the deadline tick. It still
        # cannot release C4 before the existing motor owner has completed.
        if ep.recovery_arrival:
            ep.recovery_decision['result'] = 'confirmed_arrival_no_further_motion'
            self._recordRecoveryDecision()
            return
        # A refusal is not a physical diagnosis. Let the normal feeder and
        # perception workers produce a new paired observation without moving.
        if now >= ep.recovery_deadline_mono and self._discardUnverifiedHandoff(now, boundary, observed):
            return
        refresh_after = ep.release_evidence.get('refusal_refresh_after')
        if (not all(observed['predicates'].get(k) for k in
                    ('feeder_tick_fresh', 'c3_frame_fresh', 'c4_frame_fresh'))
                or refresh_after is not None and
                   (observed.get('frame_ts', 0) <= refresh_after
                    or boundary['frame_ts'] <= refresh_after)):
            ep.release_evidence.setdefault('refusal_refresh_after', observed.get('at_wall', time.time()))
            ep.recovery_branch = 'refresh_refusal'
            ep.recovery_decision['blocking_predicates'] = [k for k in
                ('feeder_tick_fresh','c3_frame_fresh','c4_frame_fresh')
                if not observed['predicates'].get(k)]
            ep.recovery_decision['result'] = ('awaiting_terminal_physical_evidence'
                if now >= ep.recovery_deadline_mono else 'awaiting_fresh_recovery_observation')
            self._recordRecoveryDecision()
            return
        if refresh_after is not None:
            ep.release_evidence.pop('refusal_refresh_after')
        if ep.recovery_branch == 'refresh_refusal':
            # Only the original identity may authorize motion after a refusal.
            if observed.get('retained_plan') is not None:
                self._advanceRetainedPiece(now, boundary, observed, last)
            elif not (now >= ep.recovery_deadline_mono and
                      self._discardUnverifiedHandoff(now, boundary, observed)):
                ep.recovery_decision['result'] = 'observing_unlocated_arrival'
                self._recordRecoveryDecision()
            return
        if (observed.get('retained_plan') is not None and
                (now >= ep.recovery_deadline_mono
                 or not observed['predicates'].get('spatial_association')
                 or any(l.get('retained') or l.get('kind') == 'jitter'
                        for l in ep.current_recovery_legs))):
            self._advanceRetainedPiece(now, boundary, observed, last)
            return
        if now >= ep.recovery_deadline_mono:
            # The ceiling ends motion authority, not camera observation. Decide
            # from the original piece's identity, not a reseeded spatial envelope.
            if self._discardUnverifiedHandoff(now, boundary, observed):
                return
            if (observed['predicates'].get('c3_frame_fresh')
                    and self._retainedPieceVisible(observed)):
                ep.recovery_decision['blocking_predicates'] = ['retained_c3_piece_after_bounded_recovery']
                self._unresolvedTransfer('original C3 piece remains visible after bounded physical recovery')
            else:
                # Stale frames or unfinished owned motion cannot prove arrival,
                # disappearance, or a physical obstruction. No new motion/budget.
                ep.recovery_decision['result'] = 'awaiting_terminal_physical_evidence'
            self._recordRecoveryDecision()
            return
        if self._arrival_presence_streak:
            ep.recovery_decision['result'] = 'observing_partial_arrival'
            self._recordRecoveryDecision()
            return
        # C3 disappearance may mean transport succeeded before the C4 detector
        # settled (including a temporary merged box outside the intake zone).
        # Missing support forbids agitation, not further camera observation.
        # Keep the same reserved pocket and active 12-second ceiling; no new
        # release, motor command, or weakened arrival predicate is authorized.
        if (not observed.get('same_piece_retained') and
                not all(observed['predicates'][k] for k in ('spatial_association', 'c3_supported_region'))):
            ep.recovery_decision['result'] = 'observing_unlocated_arrival'
            self._recordRecoveryDecision()
            return
        if ep.recovery_branch == 'observation_only' or any(leg.get('dispatch_refused') == 'leg_within_time_budget'
               for leg in ep.current_recovery_legs):
            ep.recovery_decision['result'] = 'observing_handoff_without_further_motion'
            self._recordRecoveryDecision()
            return
        last = ep.current_recovery_legs[-1] if ep.current_recovery_legs else None
        if last:
            if last['accepted'] is not True:
                self._unresolvedTransfer("recovery command acceptance uncertain or rejected")
                self._recordRecoveryDecision()
                return
            if not observed['predicates']['motor_c3_resolved']:
                ep.recovery_decision['result'] = 'wait_for_owned_completion'
                self._recordRecoveryDecision()
                return
            if last['completed_at_mono'] is None:
                last['completed_at_mono'] = now
                last['completed_at_wall'] = time.time()
            if (observed.get('frame_ts', 0) <= last['completed_at_wall'] or
                    boundary['frame_ts'] <= last['completed_at_wall']):
                ep.recovery_decision['result'] = 'wait_for_post_motion_frames'
                self._recordRecoveryDecision()
                return
        # One operation, not fixed recovery stages. The feeder re-observes the
        # leader and followers before each uniquely recorded command. A stopped
        # motor is never evidence that the piece reached its intended endpoint.
        number = len(ep.current_recovery_legs)+1
        action = {'stage': 1, 'key': ('terminal.' if ep.terminal_recovery_active else '') + f'{number}.forward'}
        use_jitter = observed.get('same_piece_retained') and (
            not observed['predicates']['c3_supported_region']
            or any(l.get('kind') == 'jitter' for l in ep.current_recovery_legs)
            or observed.get('observed_progress_deg') is None
            or observed['observed_progress_deg'] <= 2.0)
        if use_jitter:
            count = sum(l.get('kind') == 'jitter' for l in ep.recovery_legs)
            action = {'stage': 2, 'key': f'{count+1}.jitter', 'kind': 'jitter'}
        ep.recovery_branch = 'retained_piece_jitter' if use_jitter else 'exit_clearance'
        result = self.shared.request_c3_recovery(ep, boundary, action=action)
        self._recordRecoveryDecision()
        if result['result'] in ('unsafe', 'rejected', 'acceptance_unknown'):
            reasons = result.get('blocking_predicates', [result['result']])
            if (result['result'] == 'unsafe' and not result.get('command')
                    and set(reasons) & {'feeder_tick_fresh','c3_frame_fresh','c4_frame_fresh',
                                        'forward_sweep_clear','positive_safe_travel','reverse_sweep_clear'}):
                ep.release_evidence['refusal_refresh_after'] = result['at_wall']
                ep.recovery_branch = 'refresh_refusal'
                ep.recovery_decision['result'] = 'awaiting_fresh_recovery_observation'
                self._recordRecoveryDecision()
                return
            command = result.get('command')
            if (result['result'] == 'unsafe' and reasons and set(reasons) <=
                    {'observed_forward_progress', 'leg_within_time_budget', 'c4_empty', 'feeder_tick_fresh',
                     'motor_c2_resolved', 'motor_c3_resolved', 'c3_frame_fresh',
                     'c4_frame_fresh', 'post_motion_frames'} and
                    (not command or command.get('dispatch_refused') == 'leg_within_time_budget')):
                # No further motion is justified, but C4 may already contain
                # the released piece. Use the remaining existing observation
                # budget instead of freezing before confirmation can settle.
                # A leg that cannot fit also forbids motion, not observation:
                # the unchanged deadline still determines terminal disposition.
                if set(reasons) & {'leg_within_time_budget', 'observed_forward_progress'}:
                    ep.recovery_branch = 'observation_only'
                ep.recovery_decision['result'] = 'observing_handoff_without_further_motion'
                self._recordRecoveryDecision()
                return
            self._unresolvedTransfer("recovery refused: " + ", ".join(reasons))
            self._recordRecoveryDecision()
        elif result['result'] == 'accepted':
            # Same reservation and episode. Every next leg requires new frames.
            self._arrival_armed_at_mono = now
            self._arrival_armed_at_wall = time.time()
            self._arrival_last_frame_ts = 0.0
            self._arrival_empty_streak = self._arrival_presence_streak = 0
            self._arrival_samples = []
            self._arrival_timed_out = False
            self.noteProgress()

    def _advanceRetainedPiece(self, now, boundary, observed, last):
        """Continue the same positively identified load through its saved exit."""
        ep = self._episode
        if now >= ep.recovery_deadline_mono and not observed['predicates'].get('motor_c2_resolved'):
            self._unresolvedTransfer('C2 motor has not completed its owned command')
        elif last and last.get('accepted') is not True and not last.get('dispatch_refused'):
            self._unresolvedTransfer('retained recovery command acceptance uncertain or rejected')
        elif (self._arrival_presence_streak or
              not all(observed['predicates'].get(k) for k in
                      ('motor_c2_resolved','motor_c3_resolved','c3_frame_fresh','c4_frame_fresh','feeder_tick_fresh'))):
            ep.recovery_decision['result'] = 'observing_retained_piece_without_motion'
        elif observed['retained_plan']['reason']:
            self._unresolvedTransfer(self._retainedStopReason(observed['retained_plan']))
        else:
            completion = last.get('completed_at_wall') if last else ep.release_evidence.get('completion_observed_wall')
            if (completion is None or observed.get('frame_ts',0) <= completion
                    or boundary['frame_ts'] <= completion):
                ep.recovery_decision['result'] = 'wait_for_post_motion_frames'
                self._recordRecoveryDecision()
                return
            jitter = observed['retained_plan']['jitter']
            count = sum(l.get('kind') == 'jitter' for l in ep.recovery_legs)
            action = ({'stage':2,'key':f'{count+1}.jitter','kind':'jitter'} if jitter else
                      {'stage':1,'key':('terminal.' if ep.terminal_recovery_active else '')+
                       f'{len(ep.current_recovery_legs)+1}.forward'})
            action['retained'] = True
            ep.recovery_branch = 'retained_observed_path'
            result = self.shared.request_c3_recovery(ep,boundary,action=action)
            if result['result'] in ('unsafe', 'wait') and not all(
                    result['predicates'].get(k) for k in
                    ('feeder_tick_fresh','c3_frame_fresh','c4_frame_fresh')):
                ep.release_evidence['refusal_refresh_after'] = result['at_wall']
                ep.recovery_branch = 'refresh_refusal'
                ep.recovery_decision['result'] = 'awaiting_fresh_recovery_observation'
            elif result['result']=='unsafe' and not result.get('retained_plan'):
                # Identity/freshness changed during the dispatch recheck. No
                # motion is permitted; the existing terminal path decides loss.
                ep.recovery_decision['result'] = 'observing_unlocated_arrival'
            elif result['result'] in ('unsafe','rejected','acceptance_unknown'):
                if (result['result'] == 'unsafe' and not result.get('command')
                        and set(result.get('blocking_predicates', [])) <= {
                            'spatial_association','c3_supported_region','forward_sweep_clear',
                            'reverse_sweep_clear','positive_safe_travel','retained_path_available',
                            'recovery_budget','leg_within_time_budget','followthrough_endpoint_available',
                            'same_piece_retained','post_motion_frames'}):
                    # Dispatch saw different geometry. Reobserve before declaring
                    # a physical obstruction; never execute the refused command.
                    ep.release_evidence['refusal_refresh_after'] = result['at_wall']
                    ep.recovery_branch = 'refresh_refusal'
                    ep.recovery_decision['result'] = 'awaiting_fresh_recovery_observation'
                else:
                    self._unresolvedTransfer('retained recovery motion unavailable: '+', '.join(
                        result.get('blocking_predicates',[result['result']])))
            elif result['result'] == 'accepted':
                self._arrival_armed_at_mono = now
                self._arrival_armed_at_wall = time.time()
                self._arrival_last_frame_ts = 0.0
                self._arrival_empty_streak = self._arrival_presence_streak = 0
                self._arrival_samples = []
                self._arrival_timed_out = False
                self.noteProgress()
        self._recordRecoveryDecision()

    @staticmethod
    def _retainedStopReason(plan):
        followers = ','.join(str(p['id']) for p in plan['followers']) or 'none'
        return (f"Original C3 track {plan['piece']['id']} remains at "
                f"{plan['observed_com']:.3f} degrees from exit; "
                f"forward clearance {plan['forward_clearance_deg']} degrees, "
                f"bounded jitter available={plan['jitter_available']}, "
                f"followers={followers}: {plan['reason']}")

    def _retainedPieceVisible(self, observed: dict) -> bool:
        """Only positive original identity proves C3 retention.

        A spatial envelope can follow a replacement track without registering
        loss. Continuity alone is not proof that the original piece remains.
        """
        leader = self._episode.leader_id
        original = leader is not None and any(p.get('id') == leader for p in
            (*observed.get('material', []), *observed.get('followers', [])))
        return bool(original or observed.get('same_piece_retained'))

    def _discardUnverifiedHandoff(self, now: float, boundary: dict, observed: dict) -> bool:
        """Carry uncertainty in the original FIFO slot after bounded observation.

        This never confirms arrival or releases an outstanding motor command.
        Positively retained material keeps normal recovery. Unassociated C3
        detections do not prove that the original transfer remains on C3.
        """
        ep = self._episode
        predicates = observed['predicates']
        required = ('active_episode', 'episode_open', 'c3_enabled', 'motors_unsuppressed',
                    'c3_owner_consistent', 'motor_c3_resolved', 'c3_frame_fresh',
                    'reserved_boundary', 'c4_available',
                    'c4_stopped_aligned', 'c4_frame_fresh')
        if (not all(predicates.get(k) for k in required)
                or ep.recovery_arrival or self._confirmed_recovery_samples
                or self._retainedPieceVisible(observed)
                or getattr(self.shared, 'c3_motion_pending', False)):
            return False
        motion_legs = [leg for leg in ep.current_recovery_legs
                       if leg.get('dispatch_refused') != 'leg_within_time_budget']
        completion = (motion_legs[-1].get('completed_at_wall')
                      if motion_legs else
                      ep.release_evidence.get('completion_observed_wall'))
        if (completion is None or observed.get('frame_ts', 0) <= completion
                or boundary.get('frame_ts', 0) <= completion
                or any(leg.get('accepted') is not True
                       and leg.get('dispatch_refused') != 'leg_within_time_budget'
                       for leg in ep.recovery_legs)):
            return False
        ep.forced_reject_reason = UNVERIFIED_C4_HANDOFF
        ep.group_size_unknown = True
        ep.recovery_decision['final_outcome'] = 'unverified_discard_bound'
        self._clearArrivalArm()
        self._admit(now, unverified=True)
        self._recordRecoveryDecision()
        return True

