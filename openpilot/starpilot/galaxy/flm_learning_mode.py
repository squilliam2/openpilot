"""Parked, compare-and-swap selection of the existing qualified live learner."""

import hashlib

from opendbc.car.structs import car
from openpilot.starpilot.lateral.learning_preferences import (
  KEY, MAX_BYTES, LEGACY_KEY, learning_mode, read_preference, replace_learning_mode,
)
from openpilot.starpilot.saved_document import commit_exact
from openpilot.starpilot.saved_source import read_saved
from openpilot.starpilot.schema_cache import inspect_cache


class LearningMode:
  def __init__(self, params, *, parked):
    self.params, self.parked = params, parked

  def _read(self):
    offroad = read_saved(self.params, 'IsOffroad', 1)
    if offroad[0] not in (b'0', b'1') or not offroad[1]:
      raise ValueError('Current vehicle state is unavailable')
    cp_key = 'CarParamsPersistent' if offroad[0] == b'1' else 'CarParams'
    keys = ((cp_key, 1024 * 1024), (KEY, MAX_BYTES), (LEGACY_KEY, 1), ('IsOffroad', 1))
    values = {key: read_saved(self.params, key, limit) for key, limit in keys}
    if any(not okay for raw, okay in values.values()) or values[cp_key][0] is None:
      raise ValueError('Saved vehicle configuration is unavailable')
    payload = inspect_cache(cp_key, values[cp_key][0]).payload if offroad[0] == b'1' else values[cp_key][0]
    if payload is None:
      raise ValueError('The vehicle must be identified before selecting live learning')
    with car.CarParams.from_bytes(payload) as cp:
      cp = cp.as_builder()
    if cp.notCar or cp.passive or cp.dashcamOnly or str(cp.steerControlType) != 'torque':
      raise ValueError('Live FLM requires a supported torque-actuated vehicle')
    preference = read_preference(self.params, cp, preference_snapshot=values[KEY], legacy_snapshot=values[LEGACY_KEY])
    if not preference.valid:
      raise ValueError('Learning preferences need review')
    revision = hashlib.sha256(repr(values).encode()).hexdigest()
    return cp, keys, values, preference, revision

  def snapshot(self):
    try:
      from opendbc.car.vin import VIN_UNKNOWN, is_valid_vin
      cp, keys, values, preference, revision = self._read()
      if any(read_saved(self.params, key, limit) != values[key] for key, limit in keys):
        raise ValueError('Vehicle configuration changed')
      return {'version': 1, 'available': True, 'parked': bool(self.parked()), 'vehicle': str(cp.carFingerprint),
              'mode': learning_mode(values[KEY][0], cp), 'enabled': preference.enabled, 'revision': revision,
              'identitySource': 'original_vin' if is_valid_vin(str(cp.carVin)) and str(cp.carVin) != VIN_UNKNOWN else 'device_firmware_fallback'}
    except Exception:
      return {'version': 1, 'available': False, 'parked': False, 'vehicle': None, 'mode': None, 'enabled': None, 'revision': None}

  def select(self, mode, revision, *, authorized):
    if not authorized():
      raise PermissionError('Galaxy session expired')
    if not self.parked():
      raise ValueError('Select the learning mode while parked; it starts on the next drive')
    cp, keys, values, preference, current = self._read()
    if revision != current:
      raise ValueError('Vehicle or learning preferences changed; refresh before selecting a mode')
    raw = replace_learning_mode(values[KEY][0], cp, mode)

    def permitted():
      return authorized() and self.parked() and all(read_saved(self.params, key, limit) == values[key] for key, limit in keys if key != KEY)

    result = commit_exact(self.params, key=KEY, max_bytes=MAX_BYTES, raw=raw, expected=values[KEY][0],
                          authorized=permitted, temp_prefix='.learning-mode-')
    if not result.verified:
      raise ValueError('Learning mode could not be saved; refresh before retrying')
    return self.snapshot()
