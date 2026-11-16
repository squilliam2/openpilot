"""Declared observations test handoff/interval isolation, not road improvement."""

from dataclasses import replace
import bz2
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from opendbc.car import structs
from openpilot.cereal import log, messaging
from openpilot.common.params import Params
from openpilot.starpilot.flm.canonical_surface_adapter import NAMES
from openpilot.starpilot.flm.driver_exclusions import DriverExclusions
from openpilot.starpilot.flm.live_parked_finish import ParkedCandidate, LiveParkedFinisher, fit_candidate, load_candidate
from openpilot.starpilot.flm.offroad_processor import collect_route_inputs
from openpilot.starpilot.flm.offroad_route import RouteReceipt
from openpilot.starpilot.flm.offroad_store import RouteScope
from openpilot.starpilot.flm.tests.test_live_tracking import optimizer, ready
from openpilot.starpilot.schema_cache import put_cache


def candidate_for(owner):
  b = owner.binding
  scope = RouteScope(b.cp_sha256, b.controller_revision, b.baseline_revision, b.persistent_source_epoch,
                     b.reset_generation, 'starpilot', owner.domain)
  return ParkedCandidate(scope, tuple(owner.accepted.items()), 'ff_gain_left', .005, 'a' * 64).validate()


def test_parked_intent_never_creates_an_offer_without_fresh_native_episodes():
  owner = optimizer()
  owner.parked_candidate = candidate_for(owner)
  assert owner.preferred_probe_group(1_000_000_000) == 'ff.left'
  assert not owner._propose(1_000_000_000)
  assert owner.offer_state is None and not owner.before and not owner.future
  # A preferred group cannot monopolize the numerical worker indefinitely.
  assert owner.preferred_probe_group(62_000_000_000) is None
  state, _ = ready(owner, native=1)
  assert state is None


def test_fresh_live_trial_still_requires_independent_future_and_driver_input_rolls_it_back():
  owner = optimizer()
  owner.parked_candidate = candidate_for(owner)
  state, _ = ready(owner)
  assert state is not None and state['phase'] == 'trial' and not state.get('accepted')
  clean_baseline = tuple(owner.baseline)
  owner.interrupt('driver_override')
  assert tuple(owner.baseline) == clean_baseline
  assert owner.offer_state is None or owner.offer_state['phase'] == 'rollback'
  assert not any(owner.accepted.values())
  assert not owner.future


@pytest.mark.parametrize('field,value', [('reset_generation', 'changed'), ('cp_sha256', 'c' * 64),
                                        ('controller_revision', 'other-controller'), ('persistent_source_epoch', 'd' * 64)])
def test_old_vehicle_settings_or_reset_cannot_select_a_parked_candidate(field, value):
  owner = optimizer()
  original = candidate_for(owner)
  owner.parked_candidate = replace(original, scope=replace(original.scope, **{field: value}))
  assert owner.preferred_probe_group(1_000_000_000) is None
  owner.parked_candidate = original
  owner.accepted['center_taper_max'] = .005
  assert owner.preferred_probe_group(1_000_000_000) is None


def test_real_disjoint_parked_fits_produce_only_a_source_bound_candidate():
  from openpilot.starpilot.flm.tests.test_offroad_backend import TestOffroadTrackingPolicy as _TrackingFixtures
  owner = optimizer()
  declared = candidate_for(owner)
  windows = _TrackingFixtures.windows('declared-training', .85) + _TrackingFixtures.windows('declared-heldout', .85)
  metadata = {'cp_sha256': declared.scope.cp_sha256, 'controller_revision': declared.scope.controller_revision,
              'baseline_revision': declared.scope.baseline_revision, 'controller_mode': declared.scope.controller_mode}
  inputs = SimpleNamespace(windows={'context': windows}, context_metadata={'context': metadata}, receipt=SimpleNamespace(source_sha256='f' * 64))
  candidate = fit_candidate(inputs, declared.scope, declared.previous, permitted=lambda: True)
  assert candidate is not None and candidate.name == 'ff_gain_left' and 0 < candidate.delta <= .005
  assert candidate.source_sha256 == 'f' * 64
  assert candidate.document()['requires_fresh_live_qualification'] is True
  # A parked relative map or different actually applied live profile is not
  # relabeled as the native/confirmed canonical baseline.
  metadata['route_revision'] = 'route-reference-relative-v3'
  assert fit_candidate(inputs, declared.scope, declared.previous, permitted=lambda: True) is None
  del metadata['route_revision']
  metadata['live_coordinates'] = tuple(.001 for _ in NAMES)
  assert fit_candidate(inputs, declared.scope, declared.previous, permitted=lambda: True) is None


def _events(segment, cp, driver_stamps):
  first = 1_000_000_000 + segment * 12_000_000_000
  result = [log.Event.new_message(logMonoTime=first, valid=True, carParams=cp)]
  for i in range(1200):
    stamp = first + i * 10_000_000
    cs = log.Event.new_message(logMonoTime=stamp, valid=True)
    cs.init('carState').vEgo = 20.
    cs.carState.canValid = True
    cs.carState.gasPressed = stamp in driver_stamps
    result.append(cs)
    cc = log.Event.new_message(logMonoTime=stamp, valid=True)
    cc.init('carControl').latActive = True
    result.append(cc)
    event = messaging.new_message('starpilotLateralState', valid=True, logMonoTime=stamp)
    event.starpilotLateralState.learningEvidence = {
      'version': 1, 'controlMonoTime': stamp, 'sourceStateMonoTime': stamp, 'sourceReceiptMonoTime': stamp,
      'enabled': True, 'active': True, 'valid': True, 'controllerMode': 'starpilot',
      'controllerRevision': 'declared-recorded-controller', 'baselineRevision': 'b' * 64, 'baselineValues': [1.],
      'stageSchemaRevision': 'torque-selected-stage-snapshot-v1',
    }
    result.append(event)
    if stamp not in driver_stamps:
      event = log.Event.new_message(logMonoTime=stamp, valid=True)
      state = event.init('controlsState').lateralControlState.init('torqueState')
      state.active, state.desiredLateralAccel, state.actualLateralAccel = True, .5, .4
      result.append(event)
  return result


def test_original_driver_pulses_between_joins_and_across_segment_boundaries_keep_clean_sections():
  cp = structs.CarParams.new_message(carFingerprint='DECLARED_DRIVER_INTERVAL_VEHICLE', steerControlType='torque')
  cp.lateralTuning.init('torque')
  pulses = {5_000_000_000, 13_050_000_000}
  def walk(root, route, *, permitted, consume):
    for segment in (0, 1):
      consume(segment, tuple(e.as_reader() for e in _events(segment, cp, pulses)))
    return RouteReceipt(route['routeId'], 'a' * 64, (('declared-0', 'b' * 64, 1), ('declared-1', 'c' * 64, 1)),
                        'd' * 64, 1_000_000_000, 25_000_000_000, 1_800_000_000_000_000_000, ())
  inputs = collect_route_inputs('.', {'routeId': 'declared-driver-input'}, permitted=lambda: True, walker=walk)
  windows = [w for rows in inputs.windows.values() for w in rows]
  assert {w.points[0].segment for w in windows} == {0, 1}
  assert any(w.points[-1].mono_ns < 4_650_000_000 for w in windows)
  assert any(6_000_000_000 < w.points[0].mono_ns and w.points[-1].mono_ns < 12_700_000_000 for w in windows)
  assert any(w.points[0].mono_ns > 14_050_000_000 for w in windows)
  assert all(not (w.points[0].mono_ns <= stamp + 1_000_000_000 and w.points[-1].mono_ns >= stamp - 350_000_000)
             for w in windows for stamp in pulses)
  assert inputs.counts['driver_input_intervals'] == 2 and inputs.counts['driver_input_excluded_windows'] > 0


def test_sustained_driver_input_uses_one_bounded_interval():
  driver = DriverExclusions()
  for stamp in range(1_000_000_000, 61_000_000_000, 10_000_000):
    driver.observe(stamp)
  driver.finish()
  assert len(driver.intervals) == 1
  assert driver.overlaps(60_500_000_000, 60_600_000_000)
  assert not driver.overlaps(62_000_000_000, 63_000_000_000)


def test_actual_isolated_parked_fitter_preserves_saved_tunes_and_skips_repeated_decode(tmp_path):
  from opendbc.car.car_helpers import interfaces
  from opendbc.car.toyota.values import CAR
  from openpilot.starpilot.flm.offroad_cache import AssessmentCache
  from openpilot.starpilot.flm.live_parked_finish import cache_directory
  from openpilot.starpilot.flm import offroad_processor
  cp = interfaces[CAR.TOYOTA_RAV4_TSS2].get_non_essential_params(CAR.TOYOTA_RAV4_TSS2)
  cp.carVin = 'JTMAB3FV0MD000001'
  params = Params(str(tmp_path / 'params'))
  params.put_bool('IsOffroad', True, block=True)
  params.put_bool('OpenpilotEnabledToggle', True, block=True)
  params.put('CurrentRoute', '00000001--aaaaaaaaaa', block=True)
  params.put('RouteCount', 2, block=True)
  put_cache(params, 'CarParamsPersistent', cp, block=True)
  root = tmp_path / 'logs'
  folder = root / '00000001--aaaaaaaaaa--0'
  folder.mkdir(parents=True)
  events = _events(0, cp, set())
  clock = log.Event.new_message(logMonoTime=13_000_000_000, valid=True)
  clock.init('clocks').wallTimeNanos = 1_800_000_000_000_000_000
  end = log.Event.new_message(logMonoTime=13_000_000_000, valid=True)
  end.init('sentinel').type = 'endOfRoute'
  (folder / 'rlog.bz2').write_bytes(bz2.compress(b''.join(e.to_bytes() for e in (*events, clock, end))))
  before = {key: params.get(key) for key in ('AutomaticSteeringProfiles', 'AutomaticSteeringLiveTrials', 'SteeringLearningPreferences')}
  worker = LiveParkedFinisher(params, root, permitted=lambda: True)
  result = worker.run_once()
  assert result['finishing_completed'] is True and result['requires_fresh_live_qualification'] is True
  assert not result['actual_application_proven'] and not result['configuration_changed']
  assert before == {key: params.get(key) for key in before}
  with patch.object(offroad_processor, 'collect_latest_steering_inputs', side_effect=AssertionError('Unchanged drive must not decode again')):
    assert worker._run_once() == result
  cache = AssessmentCache(cache_directory(params))
  assert json.loads(cache._read('checkpoint.json'))['result'] == result
  assert load_candidate(params) is None  # Declared controller differs from the actual registry, so no hint is fitted.
  params.put_bool('IsOffroad', False, block=True)
  with pytest.raises(ValueError, match='parked'):
    worker._run_once()


def test_parked_process_is_reaped_immediately_when_ignition_permission_is_lost(tmp_path):
  params = Params(str(tmp_path / 'params'))
  allowed = iter((True, False))
  child = Mock()
  child.poll.return_value = None
  worker = LiveParkedFinisher(params, tmp_path, permitted=lambda: next(allowed, False))
  with patch('openpilot.starpilot.flm.live_parked_finish.subprocess.Popen', return_value=child):
    with pytest.raises(ValueError, match='paused'):
      worker.run_once()
  child.terminate.assert_called_once()
  child.wait.assert_called_once_with(timeout=2)


def test_parked_handoff_round_trip_and_malformed_target_refusal(tmp_path):
  from openpilot.starpilot.flm.offroad_cache import AssessmentCache
  from openpilot.starpilot.flm.live_parked_finish import cache_directory
  params = Params(str(tmp_path / 'params'))
  candidate = candidate_for(optimizer())
  cache = AssessmentCache(cache_directory(params))
  cache._write('candidate.json', json.dumps(candidate.document()).encode())
  assert load_candidate(params) == candidate
  bad = candidate.document()
  bad['previous'] = list(bad['previous']) + [bad['previous'][0]]
  cache._write('candidate.json', json.dumps(bad).encode())
  assert load_candidate(params) is None


def test_managed_live_default_runs_parked_finishing_then_discards_it_before_driving(tmp_path):
  from openpilot.starpilot.flm import offroad_daemon as daemon
  params = Params(str(tmp_path / 'params'))
  params.put_bool('IsOffroad', True, block=True)
  worker = Mock()
  def finish():
    params.put_bool('IsOffroad', False, block=True)
    return {'status': 'collecting', 'reason': 'declared-finish'}
  worker.run_once.side_effect = finish
  with patch.object(daemon, 'Params', return_value=params), \
       patch.object(daemon.messaging, 'SubMaster', return_value=SimpleNamespace(sock={})), \
       patch.object(daemon, 'StatusReport', return_value=Mock()), \
       patch.object(daemon, 'wait_for_parked', return_value=True), \
       patch('openpilot.starpilot.flm.learning_mode.selected_mode', return_value='live'), \
       patch('openpilot.starpilot.flm.live_parked_finish.LiveParkedFinisher', return_value=worker) as launch, \
       patch('openpilot.starpilot.flm.steerlearnerd.main') as drive:
    assert daemon._run() is True
  launch.assert_called_once()
  worker.run_once.assert_called_once()
  drive.assert_not_called()  # A clean exec must precede the next live owner.
