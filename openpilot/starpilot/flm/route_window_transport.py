"""Lossless original-window transport for parked arithmetic, never permission."""

from openpilot.starpilot.flm.offroad_evidence import RoutePoint, RouteWindow


def pack_window(window):
  if type(window) is not RouteWindow or not window.valid() or len(window.points) > 1024:
    raise ValueError('Bounded original tracking window required')
  first = window.points[0]
  return {'identity': window.identity, 'route': first.route, 'segment': first.segment,
          'domain': first.domain, 'context': first.context_key,
          'points': [[p.mono_ns, p.desired, p.actual, p.speed_mps, p.commanded_torque, p.emitted_torque,
                      p.control_header_ns, p.output_header_ns, p.active, p.driver_override, p.saturated, p.healthy]
                     for p in window.points]}


def unpack_window(value):
  if (type(value) is not dict or set(value) != {'identity', 'route', 'segment', 'domain', 'context', 'points'} or
      type(value['points']) is not list or not 20 <= len(value['points']) <= 1024 or
      any(type(p) is not list or len(p) != 12 for p in value['points'])):
    raise ValueError('Exact original tracking window transport required')
  points = tuple(RoutePoint(value['route'], value['segment'], p[0], value['domain'], value['context'], *p[1:]) for p in value['points'])
  window = RouteWindow(value['identity'], points)
  if not window.valid() or any(not p.active or not p.healthy or p.driver_override or p.saturated for p in points):
    raise ValueError('Unchanged eligible original window required')
  return window
