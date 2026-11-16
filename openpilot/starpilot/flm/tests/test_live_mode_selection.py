from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest

from opendbc.car import structs
from openpilot.common.params import Params
from openpilot.starpilot.lateral.learning_preferences import (
  KEY, learning_mode, read_preference, replace_preference, replace_learning_mode,
)
from openpilot.starpilot.flm.vehicle_identity import persistent_unit, identity_keys
from openpilot.starpilot.galaxy.flm_learning_mode import LearningMode
from openpilot.starpilot.schema_cache import put_cache


def vehicle(vin='JTMAB3FV0MD000001'):
  cp = structs.CarParams.new_message(brand='toyota', carFingerprint='DECLARED_MODE_VEHICLE', carVin=vin, steerControlType='torque')
  cp.lateralTuning.init('torque')
  return cp


def test_mode_defaults_and_off_choice_remains_independent(tmp_path):
  params = Params(str(tmp_path / 'params'))
  cp = vehicle()
  assert learning_mode(None, cp) == 'parked'
  raw = replace_learning_mode(replace_preference(None, cp, False), cp, 'live')
  params.put(KEY, raw, block=True)
  assert learning_mode(raw, cp) == 'live'
  assert not read_preference(params, cp).enabled
  assert learning_mode(replace_preference(raw, cp, True), cp) == 'live'
  restored = replace_learning_mode(raw, cp, 'parked')
  assert learning_mode(restored, cp) == 'parked' and b'"modes"' not in restored
  assert learning_mode(raw, vehicle('JTMAB3FV0MD000002')) == 'live'  # Mode is a user choice for the platform, not a tune identity.
  other = cp.as_reader().as_builder()
  other.carFingerprint = 'ANOTHER_DECLARED_VEHICLE'
  assert learning_mode(raw, other) == 'parked'
  with pytest.raises(ValueError):
    replace_learning_mode(raw, cp, 'anything')


def test_original_vin_always_precedes_fallback_and_is_never_rewritten():
  cp = vehicle()
  before = cp.to_dict()
  assert identity_keys(cp) == ()
  assert persistent_unit(cp, {}) == str(cp.carVin)
  assert cp.to_dict() == before
  cp.carVin = '0' * 17
  cp.init('carFw', 1)[0].fwVersion = b'declared firmware'
  unknown = cp.to_dict()
  assert persistent_unit(cp, {'DongleId': b'a' * 16}) != str(cp.carVin)
  assert cp.to_dict() == unknown
  cp.carVin = 'JTMAB3FV0MD000002'
  assert persistent_unit(cp, {'DongleId': b'a' * 16}) == str(cp.carVin)


def test_parked_mode_change_is_vehicle_bound_cas_and_preserves_identity(tmp_path):
  params = Params(str(tmp_path / 'params'))
  cp = vehicle()
  params.put_bool('IsOffroad', True, block=True)
  params.put_bool('ForceAutoTuneOff', True, block=True)
  put_cache(params, 'CarParamsPersistent', cp, block=True)
  original_cp = params.get('CarParamsPersistent')
  allowed = [True]
  owner = LearningMode(params, parked=lambda: allowed[0])
  before = owner.snapshot()
  assert before['mode'] == 'parked' and before['identitySource'] == 'original_vin'
  with pytest.raises(PermissionError):
    owner.select('live', before['revision'], authorized=lambda: False)
  after = owner.select('live', before['revision'], authorized=lambda: True)
  assert after['mode'] == 'live' and after['enabled'] is False
  assert params.get('CarParamsPersistent') == original_cp
  assert params.get_bool('ForceAutoTuneOff')
  with pytest.raises(ValueError, match='changed'):
    owner.select('parked', before['revision'], authorized=lambda: True)
  allowed[0] = False
  with pytest.raises(ValueError, match='parked'):
    owner.select('parked', after['revision'], authorized=lambda: True)


def test_manager_dispatches_one_live_publisher_and_reexecs_before_parking(tmp_path):
  from openpilot.starpilot.flm import offroad_daemon as daemon
  params = Params(str(tmp_path / 'params'))
  params.put_bool('IsOffroad', False, block=True)
  def live(*, stopped):
    assert not stopped()
    params.put_bool('IsOffroad', True, block=True)
    assert stopped()
  with patch.object(daemon, 'Params', return_value=params), \
       patch.object(daemon.messaging, 'SubMaster', return_value=SimpleNamespace(sock={})), \
       patch.object(daemon, 'StatusReport', return_value=Mock()), \
       patch('openpilot.starpilot.flm.learning_mode.selected_mode', return_value='live'), \
       patch('openpilot.starpilot.flm.steerlearnerd.main', side_effect=live) as launch, \
       patch('openpilot.starpilot.flm.offroad_source.record_drive') as recorder:
    assert daemon._run() is True
  launch.assert_called_once()
  recorder.assert_not_called()


def test_selected_live_mode_with_known_vin_reaches_existing_bounded_controls_path():
  from opendbc.car.car_helpers import interfaces
  from opendbc.car.toyota.values import CAR
  from openpilot.starpilot.flm.tests import test_registered_controls_global as fixtures
  from openpilot.starpilot.flm.tests.test_live_trial_global_runtime import live_controls
  factory = interfaces[CAR.TOYOTA_RAV4_TSS2]
  cp = factory.get_non_essential_params(CAR.TOYOTA_RAV4_TSS2)
  cp.carVin = 'JTMAB3FV0MD000001'
  original = fixtures.replace_preference
  def live_preference(raw, vehicle, enabled):
    return replace_learning_mode(original(raw, vehicle, enabled), vehicle, 'live')
  with patch.object(factory, 'get_non_essential_params', return_value=cp), \
       patch.object(fixtures, 'replace_preference', side_effect=live_preference):
    with live_controls() as fixture:
      assert fixture.controls.LaC.route_flm_scope is None
      assert fixture.controls.LaC.route_flm_effects is None
      proposal = fixture.initial_offer()
      for _ in range(30):
        fixture.frame(proposal)
        assert 0 < fixture.assert_applied() <= .005
      fixture.frame(proposal, driver=True)
      fixture.assert_neutral()


def test_stopped_live_loop_does_not_publish_and_reaps_its_fitter():
  from openpilot.starpilot.flm.steerlearnerd import run
  sm, pm, fitter = Mock(), Mock(), Mock()
  run(None, sm, object(), object(), pm, None, None, None, None, None, None, Mock(), fitter, None, stopped=lambda: True)
  pm.send.assert_not_called()
  sm.update.assert_not_called()
  fitter.close.assert_called_once_with('learner_parent_exit')
