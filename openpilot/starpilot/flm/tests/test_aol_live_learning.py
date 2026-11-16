"""Actual Controls/capture admission and producer paths; no physical fit authority."""

from dataclasses import replace
from unittest import mock

import pytest

from opendbc.car.car_helpers import interfaces
from opendbc.car.toyota.values import CAR
from openpilot.cereal import messaging
from openpilot.starpilot.aol.wire import decode_safety, encode_safety
from openpilot.starpilot.flm.evidence_hooks import control_message
from openpilot.starpilot.flm.learning_wire import read_control
from openpilot.starpilot.flm.tests import test_learning_producer as producer_fixtures
from openpilot.starpilot.flm.tests.test_registered_controls_global import controls_for, feed, source_packet

BASE = 400_000_000_000
DT = 10_000_000


def actual_frame(controls, tick, *, aol=True, gas=.2, accel=0., steering=False, brake=False, regen=False, native=True):
  now = BASE + tick * DT
  original_update = controls.sm.update_msgs
  def deliver(receipt, events):
    changed = []
    for event in events:
      kind = event.which()
      if kind in ('carState', 'aolAxisState', 'aolSafetyWire'):
        event = event.as_builder()
        if kind == 'carState':
          cs = event.carState
          # Current cereal has no numeric pedal-position field. Use only real
          # gasPressed/aEgo; optional numeric fixtures stay separate.
          cs.gasPressed, cs.aEgo = gas > 0, accel
          cs.steeringPressed, cs.brakePressed, cs.regenBraking = steering, brake, regen
        elif aol and kind == 'aolAxisState':
          axis = event.aolAxisState
          axis.desiredLateral, axis.lateralActive = True, native
          axis.desiredLongitudinal, axis.longitudinalActive = False, False
        elif aol and kind == 'aolSafetyWire':
          original = decode_safety(bytes(event.aolSafetyWire))
          assert original is not None
          event.aolSafetyWire = encode_safety(replace(original, requestedLateral=True, requestedLongitudinal=False,
            lateralAllowed=native, longitudinalAllowed=False,
            pandaInventory=tuple(replace(slot, controlsAllowed=native) for slot in original.pandaInventory)))
        event = messaging.log_from_bytes(event.to_bytes())
      changed.append(event)
    original_update(receipt, changed)
  with mock.patch.object(controls.sm, 'update_msgs', side_effect=deliver):
    feed(controls, now, tick, speed=30., active=True, enabled=not aol)
  if now - controls._global_fixture_source_ns >= 250_000_000:
    controls.sm.update_msgs(now / 1e9, [source_packet(controls, now, tick, (0., 0., 0.))])
    controls._global_fixture_source_ns = now
  with mock.patch('openpilot.selfdrive.controls.controlsd.time.monotonic_ns', return_value=now):
    command, _ = controls.state_control()
  runtime = controls.global_learning
  assert runtime is not None
  event = messaging.new_message('starpilotLateralState', valid=True, logMonoTime=now)
  written = control_message(event, controls.LaC, command, control_ns=now, state_ns=now, limited=False)
  assert written is not False
  evidence = read_control(messaging.log_from_bytes(event.to_bytes())) if written else None
  return command, evidence


def test_actual_controls_and_evidence_hook_allow_stable_aol_pedal_but_keep_strict_direct_capture():
  cp = interfaces[CAR.TOYOTA_RAV4_TSS2].get_non_essential_params(CAR.TOYOTA_RAV4_TSS2)
  with controls_for(cp, 'standard', True) as controls:
    for tick in range(160):
      command, evidence = actual_frame(controls, tick)
    assert command.latActive and not command.enabled and not command.longActive
    assert evidence is not None and evidence.valid and not evidence.override
    assert controls.global_learning.pedal_policy.last_reason == 'steady_aol_accelerator'
    assert controls.global_learning.pedal_policy.numeric_pedal_position_rows == 0
    assert not controls.sm.simulation and controls.global_learning.native_required and evidence.native_allowed
    assert controls.LaC.automatic_flm_effects is None
    stale = controls.global_learning.capture_evidence(control_ns=BASE + 159 * DT, state_ns=BASE + 159 * DT - 100_000_001,
                                                      active=True, limited=False, car_control=command)
    assert stale is not None and stale.override and not controls.global_learning.pedal_policy.history
    # An older/direct caller without original CC remains strict.
    direct = controls.global_learning.capture_evidence(control_ns=BASE + 159 * DT, state_ns=BASE + 159 * DT, active=True, limited=False)
    assert direct is not None and direct.override


@pytest.mark.parametrize('change', [{'gas': 0.}, {'accel': .8}, {'accel': float('nan')},
                                  {'brake': True}, {'regen': True}, {'steering': True}, {'native': False}])
def test_actual_short_interruption_resets_aol_stability_and_keeps_native_authority(change):
  cp = interfaces[CAR.TOYOTA_RAV4_TSS2].get_non_essential_params(CAR.TOYOTA_RAV4_TSS2)
  with controls_for(cp, 'standard', True) as controls:
    for tick in range(160):
      _, evidence = actual_frame(controls, tick)
    assert evidence is not None and not evidence.override
    command, interrupted = actual_frame(controls, 160, **change)
    assert interrupted is None or interrupted.override or not interrupted.valid
    if change.get('native') is False:
      assert not command.latActive and not controls.global_learning.eligible
    _, after = actual_frame(controls, 161)
    assert after is None or after.override
    assert controls.LaC.automatic_flm_effects is None


def test_actual_engaged_cruise_gas_remains_an_override_after_long_stable_input():
  cp = interfaces[CAR.TOYOTA_RAV4_TSS2].get_non_essential_params(CAR.TOYOTA_RAV4_TSS2)
  with controls_for(cp, 'standard', True) as controls:
    for tick in range(160):
      command, evidence = actual_frame(controls, tick, aol=False)
    assert command.enabled and command.latActive
    assert evidence is not None and evidence.override
    assert controls.global_learning.pedal_policy.accepted_gas_rows == 0


def test_live_physical_producer_collects_stable_aol_and_discards_only_interrupted_window():
  fixture = producer_fixtures.TestAutomaticLearningProducer()
  producer, sm = fixture.fixture()
  sm['carControl'].enabled = False
  cs = sm['carState']
  cs.gasPressed, cs.gas, cs.aEgo = True, .2, 0.
  fixture.ticks(producer, sm, 800)
  assert producer.optional_errors == 0 and len(producer.windows) == 1
  assert producer.pedal_policy.accepted_gas_rows > 0
  cs.gas = .5
  fixture.ticks(producer, sm, 1)
  assert producer.reason == 'driver_override' and not producer.pending and not producer.points
  assert len(producer.windows) == 1
  sm['carControl'].enabled = True
  fixture.ticks(producer, sm, 100)
  assert producer.reason == 'driver_override' and len(producer.windows) == 1


def test_live_producer_stale_source_cannot_preserve_aol_stability():
  fixture = producer_fixtures.TestAutomaticLearningProducer()
  producer, sm = fixture.fixture()
  sm['carControl'].enabled = False
  sm['carState'].gasPressed, sm['carState'].gas = True, .2
  fixture.ticks(producer, sm, 100)
  assert producer.pedal_policy.history
  sm.logMonoTime['carState'] = fixture.now - 100_000_001
  with mock.patch('openpilot.starpilot.flm.learning_producer.time.monotonic_ns', return_value=fixture.now), \
       mock.patch('openpilot.starpilot.flm.learning_producer.time.clock_gettime_ns', return_value=fixture.now):
    producer.update(sm, None)
  assert producer.reason == 'source_age' and not producer.pedal_policy.history
