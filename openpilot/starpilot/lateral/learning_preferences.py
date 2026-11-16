"""Vehicle-scoped user choice, independent of learning compatibility and results."""

from dataclasses import dataclass
import json

from openpilot.starpilot.saved_source import read_saved

KEY = 'SteeringLearningPreferences'
MAX_BYTES = 4096
LEGACY_KEY = 'ForceAutoTuneOff'


@dataclass(frozen=True)
class LearningPreference:
  enabled: bool
  valid: bool
  source: str
  raw: bytes | None


def _identity(CP):
  brand, fingerprint = str(CP.brand), str(CP.carFingerprint)
  if not brand or not fingerprint or len(brand) > 64 or len(fingerprint) > 256:
    raise ValueError('Invalid vehicle identity')
  return brand, fingerprint


def _unique(pairs):
  result = {}
  for key, value in pairs:
    if key in result:
      raise ValueError('Duplicate field')
    result[key] = value
  return result


def _decode(raw):
  if raw is None:
    return {'version': 1, 'vehicles': {}}
  if not raw or len(raw) > MAX_BYTES:
    raise ValueError('Invalid preference size')
  try:
    document = json.loads(raw, object_pairs_hook=_unique)
  except RecursionError as error:
    raise ValueError('Preference nesting exceeds parser limits') from error
  if (not isinstance(document, dict) or not {'version', 'vehicles'} <= set(document) or set(document) - {'version', 'vehicles', 'modes', 'convergence'} or
      type(document['version']) is not int or document['version'] != 1 or
      not isinstance(document['vehicles'], dict)):
    raise ValueError('Invalid preference document')
  count = 0
  for brand, vehicles in document['vehicles'].items():
    if not brand or len(brand) > 64 or not isinstance(vehicles, dict):
      raise ValueError('Invalid preference brand')
    for fingerprint, enabled in vehicles.items():
      if not fingerprint or len(fingerprint) > 256 or type(enabled) is not bool:
        raise ValueError('Invalid vehicle preference')
      count += 1
  if count > 64:
    raise ValueError('Too many vehicle preferences')
  modes = document.get('modes', {})
  if type(modes) is not dict:
    raise ValueError('Invalid learning modes')
  mode_count = 0
  for brand, vehicles in modes.items():
    if not brand or len(brand) > 64 or type(vehicles) is not dict:
      raise ValueError('Invalid learning mode brand')
    for fingerprint, mode in vehicles.items():
      if not fingerprint or len(fingerprint) > 256 or mode not in ('parked', 'live'):
        raise ValueError('Invalid vehicle learning mode')
      mode_count += 1
  if mode_count > 64:
    raise ValueError('Too many vehicle learning modes')
  convergence = document.get('convergence', {})
  if type(convergence) is not dict:
    raise ValueError('Invalid convergence preferences')
  convergence_count = 0
  for brand, vehicles in convergence.items():
    if not brand or len(brand) > 64 or type(vehicles) is not dict:
      raise ValueError('Invalid convergence preference brand')
    for fingerprint, mode in vehicles.items():
      if not fingerprint or len(fingerprint) > 256 or mode not in ('standard', 'faster'):
        raise ValueError('Invalid vehicle convergence preference')
      convergence_count += 1
  if convergence_count > 64:
    raise ValueError('Too many vehicle convergence preferences')
  return document


def read_preference(params, CP, *, legacy_snapshot=None, preference_snapshot=None) -> LearningPreference:
  raw, readable = read_saved(params, KEY, MAX_BYTES) if preference_snapshot is None else preference_snapshot
  try:
    if not readable:
      raise ValueError('Unreadable preferences')
    brand, fingerprint = _identity(CP)
    vehicles = _decode(raw)['vehicles']
    if fingerprint in vehicles.get(brand, {}):
      return LearningPreference(vehicles[brand][fingerprint], True, 'scope', raw)
    legacy, readable = read_saved(params, LEGACY_KEY, 1) if legacy_snapshot is None else legacy_snapshot
    if not readable or legacy not in (None, b'0', b'1'):
      raise ValueError('Invalid legacy preference')
    return LearningPreference(legacy != b'1', True, 'default' if legacy is None else 'legacy', raw)
  except (AttributeError, TypeError, ValueError, UnicodeError):
    return LearningPreference(False, False, 'invalid', raw)


def replace_preference(raw: bytes | None, CP, enabled: bool) -> bytes:
  if type(enabled) is not bool:
    raise ValueError('Invalid preference')
  brand, fingerprint = _identity(CP)
  document = _decode(raw)
  document['vehicles'].setdefault(brand, {})[fingerprint] = enabled
  encoded = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
  _decode(encoded)
  return encoded


def _supported_torque(CP):
  return (str(getattr(CP, 'steerControlType', '')) == 'torque' and
          not any(getattr(CP, key, True) for key in ('notCar', 'passive', 'dashcamOnly')))


def learning_mode(raw, CP):
  brand, fingerprint = _identity(CP)
  document = _decode(raw)
  return document.get('modes', {}).get(brand, {}).get(fingerprint, 'live' if _supported_torque(CP) else 'parked')


def read_learning_mode(params, CP):
  raw, okay = read_saved(params, KEY, MAX_BYTES)
  if not okay:
    raise ValueError('Unreadable learning mode')
  return learning_mode(raw, CP)


def replace_learning_mode(raw, CP, mode):
  if mode not in ('parked', 'live'):
    raise ValueError('Choose parked or live learning')
  brand, fingerprint = _identity(CP)
  document = _decode(raw)
  # Both choices are explicit: an offroad-only choice must survive the default.
  document.setdefault('modes', {}).setdefault(brand, {})[fingerprint] = mode
  encoded = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
  _decode(encoded)
  return encoded


def convergence_mode(raw, CP):
  brand, fingerprint = _identity(CP)
  document = _decode(raw)
  if not _supported_torque(CP):
    return 'standard'
  return document.get('convergence', {}).get(brand, {}).get(fingerprint, 'standard')


def read_convergence_mode(params, CP):
  raw, okay = read_saved(params, KEY, MAX_BYTES)
  if not okay:
    raise ValueError('Unreadable convergence preference')
  return convergence_mode(raw, CP)


def replace_convergence_mode(raw, CP, mode):
  if mode not in ('standard', 'faster'):
    raise ValueError('Choose standard or faster convergence')
  if mode == 'faster' and not _supported_torque(CP):
    raise ValueError('Faster convergence requires a supported torque-actuated vehicle')
  brand, fingerprint = _identity(CP)
  document = _decode(raw)
  document.setdefault('convergence', {}).setdefault(brand, {})[fingerprint] = mode
  encoded = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
  _decode(encoded)
  return encoded
