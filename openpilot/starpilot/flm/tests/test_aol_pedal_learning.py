"""Steady recorded AOL throttle is observation, never historical authority."""

import bz2
from types import SimpleNamespace

import pytest

from openpilot.cereal import log, messaging
from openpilot.starpilot.flm.driver_exclusions import AolPedalPolicy, DriverExclusions
from openpilot.starpilot.flm.full_route_review import analyze_segment
from openpilot.starpilot.flm.tests.test_power_loss_recovery import saved_vehicle


def source(**changes):
  values = {'steeringPressed': False, 'brakePressed': False, 'regenBraking': False,
            'gasPressed': True, 'aEgo': 0.}
  values.update(changes)
  return SimpleNamespace(**values)


def command(**changes):
  values = {'latActive': True, 'enabled': False, 'longActive': False}
  values.update(changes)
  return SimpleNamespace(**values)


def steady(policy, cs=None, cc=None, *, start=1_000_000_000):
  cs, cc = source() if cs is None else cs, command() if cc is None else cc
  decisions = []
  for number in range(111):
    stamp = start + number * 10_000_000
    decisions.append(policy.observe(cs, cc, state_ns=stamp, command_ns=stamp))
  return decisions


def test_aol_pedal_requires_one_second_of_original_stability_and_fresh_command():
  policy = AolPedalPolicy()
  decisions = steady(policy)
  assert all(decisions[:100]) and not any(decisions[100:])
  assert policy.accepted_gas_rows == 11 and policy.excluded_gas_rows == 100
  policy.reset()
  assert steady(policy, start=4_000_000_000)[:100] == [True] * 100


@pytest.mark.parametrize('change', [
  {'gas': float('nan')}, {'gas': float('inf')}, {'gas': -.1}, {'gas': 1.1},
  {'aEgo': .6}, {'aEgo': -.6}, {'aEgo': float('nan')},
  {'steeringPressed': True}, {'brakePressed': True}, {'regenBraking': True},
])
def test_unknown_or_manual_or_longitudinal_transient_never_earns_pedal_exception(change):
  cs = source()
  for name, value in change.items():
    setattr(cs, name, value)
  assert all(steady(AolPedalPolicy(), cs=cs))


@pytest.mark.parametrize('cc', [command(enabled=True), command(longActive=True), command(latActive=False),
                                command(enabled=0), SimpleNamespace(latActive=True), None])
def test_accelerator_exception_requires_exact_aol_only_command(cc):
  policy, cs = AolPedalPolicy(), source()
  for number in range(120):
    stamp = 1_000_000_000 + number * 10_000_000
    assert policy.observe(cs, cc, state_ns=stamp, command_ns=stamp)


def test_stale_invalid_and_repeated_source_rows_cannot_advance_stability():
  policy, cs, cc = AolPedalPolicy(), source(), command()
  for _ in range(200):
    assert policy.observe(cs, cc, state_ns=1_000_000_000, command_ns=1_000_000_000)
  assert policy.accepted_gas_rows == 0
  assert all(steady(policy, start=2_000_000_000)[:100])
  for fields in ({'command_ns': 1}, {'command_valid': False}, {'state_valid': False}):
    assert policy.observe(cs, cc, **{'state_ns': 5_000_000_000, 'command_ns': 5_000_000_000} | fields)
  del cs.aEgo
  assert policy.observe(cs, cc, state_ns=6_000_000_000, command_ns=6_000_000_000)


def test_regressed_clock_or_changed_input_at_same_clock_cannot_clear_release_event():
  policy, cs, cc = AolPedalPolicy(), source(), command()
  steady(policy, cs, cc)
  cs.gasPressed = False
  stamp = 2_110_000_000
  assert policy.observe(cs, cc, state_ns=stamp, command_ns=stamp)
  assert policy.observe(cs, cc, state_ns=stamp - 10_000_000, command_ns=stamp - 10_000_000)
  cs.brakePressed = True
  assert policy.observe(cs, cc, state_ns=stamp, command_ns=stamp)
  cs.brakePressed = False
  assert policy.observe(cs, cc, state_ns=stamp, command_ns=stamp)


def test_pedal_step_and_release_create_exclusion_events_and_requalify_from_new_source_time():
  policy, cs, cc = AolPedalPolicy(), source(), command()
  steady(policy, cs, cc)
  cs.gas = .3
  stamp = 2_110_000_000
  assert policy.observe(cs, cc, state_ns=stamp, command_ns=stamp)
  assert all(steady(policy, cs, cc, start=stamp)[:100])
  cs.gasPressed = False
  release = stamp + 1_110_000_000
  assert policy.observe(cs, cc, state_ns=release, command_ns=release)
  assert policy.observe(cs, cc, state_ns=release, command_ns=release)
  exclusions = DriverExclusions()
  exclusions.observe(release)
  exclusions.finish()
  assert exclusions.overlaps(release - 300_000_000, release + 900_000_000)
  assert not policy.observe(cs, cc, state_ns=release + 10_000_000, command_ns=release + 10_000_000)


def recording(tmp_path, *, engaged=False, stale=False, pulse=None, original_override=True):
  _, cp, scope = saved_vehicle(tmp_path)
  events = [log.Event.new_message(logMonoTime=1_000_000_000, valid=True, carParams=cp)]
  for number in range(2000):
    stamp = 1_000_000_000 + number * 10_000_000
    cs = log.Event.new_message(logMonoTime=stamp, valid=True)
    cs.init('carState').vEgo, cs.carState.canValid = 20., True
    cs.carState.gasPressed, cs.carState.aEgo = True, 0.
    if pulse and 900 <= number < 910:
      if pulse == 'acceleration_step':
        cs.carState.aEgo = .45
      elif pulse == 'release':
        cs.carState.gasPressed = False
      elif pulse == 'aEgo':
        cs.carState.aEgo = 1.
      else:
        setattr(cs.carState, pulse, True)
    cc = log.Event.new_message(logMonoTime=stamp, valid=not stale)
    cc.init('carControl').latActive = True
    cc.carControl.enabled = engaged
    metadata = messaging.new_message('starpilotLateralState', valid=True, logMonoTime=stamp)
    metadata.starpilotLateralState.learningEvidence = {
      'version': 1, 'controlMonoTime': stamp, 'sourceStateMonoTime': stamp, 'sourceReceiptMonoTime': stamp,
      'enabled': True, 'active': True, 'valid': True, 'override': original_override,
      'controllerMode': scope.controller_mode, 'controllerRevision': scope.controller_revision,
      'baselineRevision': scope.baseline_revision, 'baselineValues': [1.],
      'stageSchemaRevision': 'torque-selected-stage-snapshot-v1',
    }
    state = log.Event.new_message(logMonoTime=stamp, valid=True)
    actual = state.init('controlsState').lateralControlState.init('torqueState')
    actual.active, actual.desiredLateralAccel, actual.actualLateralAccel = True, .5, .4
    events.extend((cs, cc, metadata, state))
  ending = log.Event.new_message(logMonoTime=21_000_000_000, valid=True)
  ending.init('sentinel').type = 'endOfSegment'
  events.append(ending)
  path = tmp_path / 'rlog.bz2'
  path.write_bytes(bz2.compress(b''.join(event.to_bytes() for event in events)))
  return path


def review(path):
  return analyze_segment(path, 'bz2', number=0, route_id='00000001--aaaaaaaaaa')


def test_original_steady_aol_accelerator_retains_raw_windows_without_rewriting_override_or_permission(tmp_path):
  path = recording(tmp_path)
  original = path.read_bytes()
  result = review(path)
  assert sum(row['windows'] for row in result['metrics']) > 0
  assert result['counts']['steady_aol_accelerator_rows'] > 1500
  assert result['counts']['numeric_pedal_position_rows'] == 0
  assert result['proposalGroups'] and all(row['metadataRecorded'] is False for row in result['proposalGroups'])
  assert result['nativeQualified'] is False and result['actualApplicationProven'] is False
  assert path.read_bytes() == original


@pytest.mark.parametrize('option', [{'engaged': True}, {'stale': True}])
def test_normal_engaged_or_invalid_original_commands_still_remove_accelerator_windows(tmp_path, option):
  result = review(recording(tmp_path, **option))
  assert not result['metrics'] and not result['proposalGroups']


@pytest.mark.parametrize('pulse', ['steeringPressed', 'brakePressed', 'regenBraking', 'acceleration_step', 'aEgo', 'release'])
def test_interventions_remove_only_their_original_interval_and_preserve_clean_sections(tmp_path, pulse):
  first = tmp_path / 'clean'
  first.mkdir()
  clean = review(recording(first))
  changed = review(recording(tmp_path, pulse=pulse))
  count = sum(row['windows'] for row in changed['metrics'])
  assert 0 < count < sum(row['windows'] for row in clean['metrics'])
