"""Unknown-VIN parked and live bindings must agree without broadening authority."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opendbc.car import structs
from openpilot.common.params import Params
from openpilot.starpilot.flm.offroad_runtime import configuration_scope
from openpilot.starpilot.flm.runtime_adapter_registry import car_params_digest, read_source
from openpilot.starpilot.flm.vehicle_identity import persistent_unit


def vehicle():
  cp = structs.CarParams.new_message(brand='hyundai', carFingerprint='HYUNDAI_SONATA_HYBRID', carVin='0' * 17)
  cp.lateralTuning.init('torque')
  cp.lateralTuning.torque.latAccelFactor = 2.5
  cp.lateralTuning.torque.friction = .1
  cp.init('carFw', 1)[0].fwVersion = b'declared synthetic ECU firmware'
  return cp


def test_parked_live_unknown_vin_epochs_agree_and_isolate_device_firmware_reset(tmp_path):
  cp = vehicle()
  params = Params(str(tmp_path / 'params'))
  params.put_bool('OpenpilotEnabledToggle', True, block=True)
  params.put('DongleId', 'abcdef0123456789', block=True)

  def bindings():
    descriptor = SimpleNamespace(cp_sha256=car_params_digest(cp), domain='torque', controller_mode='native', controller_revision='declared-test')
    with patch('openpilot.starpilot.flm.runtime_adapter_registry.selected_descriptor', return_value=descriptor), \
         patch('openpilot.starpilot.flm.runtime_adapter_registry.baseline_values', return_value=()), \
         patch('openpilot.starpilot.flm.runtime_adapter_registry.baseline_revision', return_value='b' * 64):
      parked, _ = configuration_scope(cp, None, None, params, manual_status='not_configured')
    live = read_source(cp, params, now_ns=1_000_000_000, calibration=(0., 0., 0.),
                       vehicle_session='declared-drive', calibration_generation='1' * 32)
    assert live.enabled and live.configuration_known
    assert parked.persistent_source_epoch == live.persistent_source_epoch
    return parked, live

  first, live = bindings()
  params.put('DongleId', '0123456789abcdef', block=True)
  second, _ = bindings()
  assert first.persistent_source_epoch != second.persistent_source_epoch
  cp.carFw[0].fwVersion = b'different declared ECU firmware'
  third, _ = bindings()
  assert second.persistent_source_epoch != third.persistent_source_epoch
  params.put('AutoSteeringLearningReset', '1', block=True)
  fourth, _ = bindings()
  assert third.persistent_source_epoch != fourth.persistent_source_epoch
  # Stored binding never substitutes for current calibration/session authority.
  no_frame = read_source(cp, params, now_ns=1_000_000_000, calibration=(0., 0., 0.), vehicle_session='declared-drive')
  assert not no_frame.persistent_source_epoch
  assert live.source_epoch != no_frame.source_epoch
  with pytest.raises(ValueError, match='manual'):
    configuration_scope(cp, None, None, params, manual_status='configured')


@pytest.mark.parametrize('device', [None, b'', b'unregistered', b'f' * 15, b'f' * 17, b'F' * 16])
def test_unknown_vin_requires_real_device_binding(device):
  with pytest.raises(ValueError, match='registered device'):
    persistent_unit(vehicle(), {'DongleId': device})


def test_unknown_vin_without_firmware_cannot_persist():
  cp = vehicle()
  cp.init('carFw', 0)
  with pytest.raises(ValueError):
    persistent_unit(cp, {'DongleId': b'a' * 16})


def test_valid_vin_retains_existing_identity_without_device():
  cp = vehicle()
  cp.carVin = '1HGCM82633A004352'  # Declared synthetic identity.
  assert persistent_unit(cp, {}) == str(cp.carVin)


def test_registered_sonata_unknown_vin_uses_same_actual_parked_and_live_scope(tmp_path):
  from opendbc.car.car_helpers import interfaces
  from opendbc.car.hyundai.values import CAR
  from openpilot.starpilot.flm.offroad_runtime import current_configuration

  cp = interfaces[CAR.HYUNDAI_SONATA_HYBRID].get_non_essential_params(CAR.HYUNDAI_SONATA_HYBRID)
  cp.carVin = '0' * 17
  cp.init('carFw', 1)[0].fwVersion = b'declared synthetic Sonata firmware'
  params = Params(str(tmp_path / 'params'))
  params.put_bool('OpenpilotEnabledToggle', True, block=True)
  params.put('DongleId', 'abcdef0123456789', block=True)
  scope, descriptor, _ = current_configuration(cp, params)
  live = read_source(cp, params, now_ns=1_000_000_000, calibration=(0., 0., 0.),
                     vehicle_session='declared-drive', calibration_generation='1' * 32)
  assert scope.cp_sha256 == descriptor.cp_sha256 == live.cp_sha256
  assert scope.persistent_source_epoch == live.persistent_source_epoch
  assert live.enabled and live.configuration_known
