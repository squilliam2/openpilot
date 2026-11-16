"""Vehicle-scoped selection; original VIN discovery remains upstream-owned."""

from openpilot.starpilot.saved_source import read_saved


def selected_mode(params):
  from capnp import KjException
  from opendbc.car.structs import car
  from openpilot.starpilot.schema_cache import inspect_cache
  from openpilot.starpilot.lateral.learning_preferences import read_learning_mode

  try:
    offroad = read_saved(params, 'IsOffroad', 1)
    key = 'CarParamsPersistent' if offroad == (b'1', True) else 'CarParams'
    raw, okay = read_saved(params, key, 1024 * 1024)
    if not okay or raw is None:
      return 'parked'
    payload = inspect_cache(key, raw).payload if key == 'CarParamsPersistent' else raw
    if payload is None:
      return 'parked'
    with car.CarParams.from_bytes(payload) as cp:
      return read_learning_mode(params, cp)
  except (AttributeError, OSError, TypeError, ValueError, KjException):
    return 'parked'
