"""Declared state-machine scenarios; real native admission is tested separately."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from openpilot.starpilot.flm.live_tracking_actor import LiveTrackingActor
from openpilot.starpilot.flm.live_trial_contract import SOURCE_EXPERIMENT_STEP, SLEW_PER_SECOND, MAX_STEP
from openpilot.starpilot.flm.offroad_worker import stronger_import_step, matches_imported_proposal
from openpilot.starpilot.flm.offroad_surface import ROUTE_NAMES
from openpilot.starpilot.flm.tests.test_tracking_source_context import owner, refresh
from openpilot.starpilot.flm.tests.test_live_tracking import episode
from openpilot.starpilot.flm.tests.test_live_parked_finish import candidate_for


def scenario(*, faster, probed, strong=True, opposite=False):
  optimizer = owner()
  optimizer.parked_candidate = candidate_for(optimizer)
  optimizer.faster_convergence = faster
  optimizer.import_faster_supported = strong
  optimizer.faster_source_probe_enabled = faster
  for number in range(20):
    sample = episode(optimizer, number, 'ff_gain_left', kind='large')
    feature = replace(sample.sensitivity[0], error_feature_sum=-.05 if opposite else .05,
                      minus_probed_step=probed, plus_probed_step=probed)
    sample = replace(sample, sensitivity=(feature,), normalization_accel=max(.1, sample.goal_rms))
    refresh(optimizer, sample.end_ns)
    optimizer.observe_episode(sample, now_ns=sample.end_ns)
    if optimizer.offer_state is not None:
      break
  return optimizer


def test_stronger_mode_requires_strong_route_agreement_and_actual_larger_native_probe():
  normal = scenario(faster=False, probed=SOURCE_EXPERIMENT_STEP)
  fast = scenario(faster=True, probed=2 * SOURCE_EXPERIMENT_STEP)
  assert normal.offer_state is not None and fast.offer_state is not None
  assert normal.offer_state['target']['ff_gain_left'] == SOURCE_EXPERIMENT_STEP
  assert fast.offer_state['target']['ff_gain_left'] == 2 * SOURCE_EXPERIMENT_STEP
  assert fast.offer_state['import_source_sha256'] == fast.parked_candidate.source_sha256
  assert fast.before and fast.future == [] and not any(fast.accepted.values())
  assert not fast.offer_state['accepted']
  assert MAX_STEP == .05 and SLEW_PER_SECOND == .005
  assert scenario(faster=True, probed=SOURCE_EXPERIMENT_STEP).offer_state is None
  assert scenario(faster=True, probed=2 * SOURCE_EXPERIMENT_STEP, strong=False).offer_state is None


def test_larger_probe_cannot_offer_unmeasured_smaller_opposite_or_mixed_trial():
  assert scenario(faster=True, probed=.02, opposite=True).offer_state is None
  assert scenario(faster=False, probed=.02).offer_state is None
  assert scenario(faster=True, probed=0.).offer_state is None


def test_interrupted_larger_selector_cannot_recover_using_smaller_native_probes():
  optimizer = scenario(faster=True, probed=.02)
  assert optimizer.selector_hint['source_probe_step'] == .02
  target = optimizer.offer_state['target']
  sample = episode(optimizer, 25, 'ff_gain_left', kind='large')
  feature = replace(sample.sensitivity[0], minus_probed_step=.01, plus_probed_step=.01)
  sample = replace(sample, sensitivity=(feature,), normalization_accel=max(.1, sample.goal_rms))
  assert optimizer._fresh_selected_effect((sample,), 'ff_gain_left', target, source_probe_step=.02) is None
  larger = replace(sample, sensitivity=(replace(feature, minus_probed_step=.02, plus_probed_step=.02),))
  assert optimizer._fresh_selected_effect((larger,), 'ff_gain_left', target, source_probe_step=.02) is not None
  optimizer._discard_selector()
  assert optimizer.selector_hint is None


def test_offroad_strength_is_fixed_before_checks_and_stays_in_existing_envelope():
  current = dict.fromkeys(ROUTE_NAMES, 0.)
  proposal = {'parameter': 'ff_gain_left', 'delta': .005, 'values': dict(current, ff_gain_left=.005)}
  stronger = stronger_import_step(proposal, current, 'ff_gain_left')
  assert stronger['values']['ff_gain_left'] == .01 and stronger['delta'] == .01
  assert current['ff_gain_left'] == 0. and proposal['delta'] == .005
  coupled = dict(proposal, values=dict(proposal['values'], ff_gain_right=.005))
  assert stronger_import_step(coupled, current, 'ff_gain_left') is coupled


@pytest.mark.parametrize('parameter', ['ff_gain_left', 'stable:ff_gain_left'])
def test_original_and_stable_single_coordinate_proposals_match_the_same_import(parameter):
  current = dict.fromkeys(ROUTE_NAMES, 0.)
  hint = SimpleNamespace(name='ff_gain_left', delta=.005)
  proposal = {'parameter': parameter, 'delta': .005, 'values': dict(current, ff_gain_left=.005)}
  assert matches_imported_proposal(proposal, current, hint)
  assert not matches_imported_proposal(dict(proposal, delta=-.005), current, hint)
  assert not matches_imported_proposal(dict(proposal, values=dict(proposal['values'], ff_gain_right=.005)), current, hint)
  assert not matches_imported_proposal(dict(proposal, parameter='stable:ff_gain_left,ff_gain_right'), current, hint)


@pytest.mark.parametrize('phase,accepted,ack,outcome,expected', [
  ('trial', False, False, None, 'native_trial_offered'),
  ('confirmed', True, True, None, 'confirmed'),
  ('confirmed', True, False, None, None),
  ('rollback', True, True, 'rejected', 'rolled_back'),
  ('rollback', True, True, 'interrupted', 'queued'),
])
def test_only_fresh_native_lifecycle_can_report_application(phase, accepted, ack, outcome, expected):
  events = []
  actor = cast(Any, object.__new__(LiveTrackingActor))
  actor.route_import = SimpleNamespace(intent_id='i', source_sha256='a' * 64)
  actor.import_notifications = set()
  actor.import_observer = lambda identity, state: events.append((identity, state)) or True
  state = {'trial_id': 'trial', 'phase': phase, 'accepted': accepted, 'import_source_sha256': 'a' * 64,
           'import_outcome': outcome}
  actor.ingress = SimpleNamespace(optimizer=SimpleNamespace(offer_state=state, report=lambda now: {
    'bindingCurrent': True, 'applicationAcknowledged': ack}))
  actor.observe_import(100)
  actor.observe_import(101)
  assert events == ([] if expected is None else [('i', expected)])
