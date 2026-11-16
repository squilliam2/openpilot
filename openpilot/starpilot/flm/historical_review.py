"""Observed tracking from old recordings. Never creates a tune or live receipt."""

from bisect import bisect_right
import hashlib
import json
import math
import os
from pathlib import Path
import sys

from openpilot.starpilot.flm.log_decode import decode_full_rlog_segment, LogDecodeCancelled, LogDecodeError
from openpilot.starpilot.galaxy.drive_analysis import _closed_source, _route_names
from openpilot.starpilot.galaxy.drive_history import SEGMENT_NAME

VERSION = 1
MAX_SELECTED = 14
RETAIN = frozenset(('carParams', 'initData', 'carState', 'carControl', 'controlsState', 'starpilotLateralState', 'sentinel'))


def selected_rows(rows):
  """Input-only selection; first/last plus evenly spread original segments."""
  if len(rows) <= MAX_SELECTED:
    return rows
  return [rows[n * (len(rows) - 1) // (MAX_SELECTED - 1)] for n in range(MAX_SELECTED)]


def summarize(events, groups, counts):
  """Two-second uninterrupted active spans; all results are observational."""
  from openpilot.starpilot.flm.runtime_adapter_registry import car_params_digest
  from openpilot.starpilot.flm.live_tracking_episodes import tracking_reversal_rate

  cars, commands, contexts = [], [], []
  vehicle = None
  known_vin = False
  for event in events:
    kind, stamp = event.which(), int(event.logMonoTime)
    if kind == 'carParams' and event.valid:
      cp = event.carParams
      contexts.append((stamp, car_params_digest(cp)))
      vehicle = str(cp.carFingerprint)
      known_vin |= str(cp.carVin) not in ('', '0' * 17)
    elif kind == 'carState' and event.valid:
      value = event.carState
      cars.append((stamp, float(value.vEgo), bool(value.steeringPressed or value.steerFaultTemporary or value.steerFaultPermanent)))
    elif kind == 'carControl' and event.valid:
      commands.append((stamp, bool(event.carControl.latActive)))
    elif kind == 'starpilotLateralState' and event.valid:
      evidence = event.starpilotLateralState.learningEvidence
      counts['controllerMetadataFrames'] += evidence.version == 1
      counts['routeScopeFrames'] += any(str(row.name).startswith('route.scope.') for row in evidence.stageValues)
  cars.sort(key=lambda row: row[0])
  commands.sort(key=lambda row: row[0])
  contexts.sort(key=lambda row: row[0])
  car_times, command_times, context_times = ([r[0] for r in rows] for rows in (cars, commands, contexts))
  pending, key = [], None
  for event in events:
    if event.which() != 'controlsState' or not event.valid:
      continue
    counts['trackingFrames'] += 1
    stamp = int(event.logMonoTime)
    indices = [bisect_right(times, stamp) - 1 for times in (car_times, command_times, context_times)]
    if min(indices) < 0:
      pending = []
      continue
    car, command, context = cars[indices[0]], commands[indices[1]], contexts[indices[2]]
    state = event.controlsState.lateralControlState
    kind = state.which()
    if kind not in ('torqueState', 'pidState'):
      pending = []
      continue
    state = getattr(state, kind)
    speed = car[1]
    if (not state.active or not command[1] or car[2] or not 0 <= stamp - car[0] <= 100_000_000 or
        not 0 <= stamp - command[0] <= 100_000_000 or not math.isfinite(speed) or not 2 <= speed <= 70):
      pending = []
      continue
    domain = 'lateral_acceleration' if kind == 'torqueState' else 'steering_angle'
    goal = float(state.desiredLateralAccel if kind == 'torqueState' else state.steeringAngleDesiredDeg)
    actual = float(state.actualLateralAccel if kind == 'torqueState' else state.steeringAngleDeg)
    if not all(math.isfinite(v) for v in (goal, actual)):
      pending = []
      continue
    current_key = (context[1], domain, 'below_15_mps' if speed < 15 else 'at_least_15_mps')
    if current_key != key or pending and not 0 < stamp - pending[-1][0] <= 50_000_000:
      pending = []
    key = current_key
    pending.append((stamp, goal - actual))
    if stamp - pending[0][0] < 2_000_000_000:
      continue
    if len(groups) >= 24 and key not in groups:
      counts['omittedContexts'] += 1
      pending = []
      continue
    errors = [p[1] for p in pending]
    duration = (pending[-1][0] - pending[0][0]) / 1e9
    row = groups.setdefault(key, {'domain': domain, 'speedRange': key[2], 'configurationHash': key[0],
                                  'windows': 0, 'samples': 0, 'seconds': 0., 'squareError': 0., 'absoluteError': 0.,
                                  'worstError': 0., 'reversalSeconds': 0.})
    row['windows'] += 1
    row['samples'] += len(errors)
    row['seconds'] += duration
    row['squareError'] += sum(e * e for e in errors)
    row['absoluteError'] += sum(abs(e) for e in errors)
    row['worstError'] = max(row['worstError'], max(map(abs, errors)))
    row['reversalSeconds'] += tracking_reversal_rate(errors, duration, 0.) * duration
    pending = []
  return vehicle, known_vin


def review(root, route, *, permitted=lambda: True, expected_device=None):
  """Review independently closed original sources, preserving gaps and omissions."""
  root = Path(root)
  route_id = route['routeId']
  if not permitted():
    raise InterruptedError('Historical review cancelled')
  before_names = _route_names(root, route_id, permitted)
  rows = sorted(route['segments'], key=lambda r: r['number'])
  if not 1 <= len(rows) <= 512:
    raise ValueError('Choose a recording with at most 512 segments')
  for row in rows:
    match = SEGMENT_NAME.fullmatch(row['segmentName'])
    if (not match or match.group('route').replace('_', '|') != route_id or int(match.group('number')) != row['number']):
      raise ValueError('Original recording selection changed')
  chosen = selected_rows(rows)
  groups, reviewed, omitted, revisions, seen, vehicles = {}, [], [], [], set(), set()
  counts = {'trackingFrames': 0, 'controllerMetadataFrames': 0, 'routeScopeFrames': 0, 'omittedContexts': 0}
  known_vin = False
  for row in chosen:
    if not permitted():
      raise InterruptedError('Historical review cancelled')
    if not row['files'].get('rlog'):
      omitted.append([row['number'], 'Original dense rlog is unavailable'])
      continue
    try:
      with _closed_source(root, row['segmentName'], row['files'], permitted, prefer_rlog=True) as (raw, codec, current, revision):
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
          raise LogDecodeError('Duplicate original segment bytes')
        events = decode_full_rlog_segment(raw, codec, retain=RETAIN, cancelled=lambda: not permitted())
        endings = [str(e.sentinel.type) for e in events if e.which() == 'sentinel' and e.valid
                   and str(e.sentinel.type) in ('endOfSegment', 'endOfRoute')]
        if len(endings) != 1 or not any(e.which() == 'carParams' and e.valid for e in events):
          raise LogDecodeError('Segment lacks a closing marker or original vehicle parameters')
        if expected_device is not None and not any(e.which() == 'initData' and str(e.initData.dongleId) == expected_device for e in events):
          raise ValueError('Downloaded recording does not match the requested device')
        if not current() or not permitted():
          raise InterruptedError('Historical source changed')
        vehicle, vin = summarize(events, groups, counts)
        if vehicle:
          vehicles.add(vehicle)
        known_vin |= vin
        reviewed.append(row['number'])
        seen.add(digest)
        revisions.append((row, revision))
        del events
    except LogDecodeCancelled:
      raise InterruptedError('Historical decode cancelled') from None
    except LogDecodeError:
      if not permitted():
        raise InterruptedError('Historical review cancelled') from None
      omitted.append([row['number'], 'Original compressed segment is incomplete or unreadable'])
  if not reviewed:
    raise ValueError('No independently closed dense segments were available')
  for row, revision in revisions:
    with _closed_source(root, row['segmentName'], row['files'], permitted, prefer_rlog=True, inspect_only=True) as (_, _, current, observed):
      if observed != revision or not current():
        raise InterruptedError('Historical source changed before publishing')
  if not permitted() or before_names != _route_names(root, route_id, permitted):
    raise InterruptedError('Historical inventory changed before publishing')
  metrics = []
  for row in groups.values():
    metrics.append({key: row[key] for key in ('domain', 'speedRange', 'configurationHash', 'windows', 'samples')} | {
      'observedSeconds': round(row['seconds'], 3), 'rmse': math.sqrt(row['squareError'] / row['samples']),
      'meanAbsoluteError': row['absoluteError'] / row['samples'], 'worstAbsoluteError': row['worstError'],
      'errorReversalHz': row['reversalSeconds'] / row['seconds'],
    })
  return {'version': VERSION, 'routeId': route_id, 'vehicles': sorted(vehicles), 'knownVinRecorded': known_vin,
          'availableSegments': len(rows), 'selectedSegments': [r['number'] for r in chosen], 'reviewedSegments': reviewed,
          'omittedSegments': omitted, 'wholeRouteReviewed': len(reviewed) == len(rows), 'counts': counts, 'metrics': metrics,
          'analysisOnly': True, 'automaticApplicationAllowed': False, 'tuneChanged': False,
          'limitation': 'Recorded tracking only. Missing configuration, pose or native replay evidence is not recreated.'}


def main():
  import ctypes
  import resource
  import signal
  parent = os.getppid()
  if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or parent == 1 or os.getppid() != parent:
    raise InterruptedError('Historical review parent is unavailable')
  os.nice(19)
  resource.setrlimit(resource.RLIMIT_CPU, (180, 180))
  resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
  resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
  request = json.loads(sys.stdin.buffer.read(128 * 1024))
  value = review(request['root'], request['route'], expected_device=request.get('expectedDevice'))
  sys.stdout.write(json.dumps(value, allow_nan=False))


if __name__ == '__main__':
  main()
