"""One original segment at a time; observations and intentions are never authority."""

import hashlib
import json
import math
import os
from pathlib import Path
import sys

from openpilot.starpilot.flm.log_decode import decode_full_rlog_segment

MAX_BYTES = 32 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
ANALYSIS_REVISION = 2


def _digest(value):
  return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _revision(info):
  return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _identity(cp, device):
  firmware = sorted((str(row.ecu), int(row.address), int(row.subAddress), bytes(row.fwVersion).hex()) for row in cp.carFw)
  return {'fingerprint': str(cp.carFingerprint), 'vin': str(cp.carVin), 'deviceId': device,
          'firmwareSha256': _digest(firmware), 'firmwareRecorded': bool(firmware),
          'cpSha256': hashlib.sha256(cp.to_bytes()).hexdigest()}


def analyze_segment(path, codec, *, number, route_id, expected_device=None, permitted=lambda: True):
  """Keep every clean window in this file; run existing fitting separately by setup.

  A caller may use the result to select a NEW experiment. No source-clock rows,
  configuration fields, applied coordinates or native permission are synthesized.
  """
  from openpilot.starpilot.flm.driver_exclusions import recorded_driver_exclusions, AFTER_NS, BEFORE_NS
  from openpilot.starpilot.flm.historical_review import summarize
  from openpilot.starpilot.flm.offroad_route import RETAIN, SelectedSourceReceipt
  from openpilot.starpilot.flm.offroad_processor import collect_route_inputs
  from openpilot.starpilot.flm.live_parked_finish import trim_unobserved_edges
  from openpilot.starpilot.flm.offroad_analysis import analyze, cohort
  from openpilot.starpilot.flm.canonical_surface_adapter import NAMES
  from openpilot.starpilot.flm.runtime_adapter_registry import car_params_digest

  if not permitted():
    raise InterruptedError('Full-route review paused')
  source = Path(path)
  if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= MAX_BYTES:
    raise ValueError('Original segment is unavailable or exceeds the per-file budget')
  before = _revision(source.stat())
  raw = source.read_bytes()
  sha = hashlib.sha256(raw).hexdigest()
  events = decode_full_rlog_segment(raw, codec, retain=RETAIN, cancelled=lambda: not permitted())
  cps = [event.carParams for event in events if event.which() == 'carParams' and event.valid]
  endings = [str(event.sentinel.type) for event in events if event.which() == 'sentinel' and event.valid and
             str(event.sentinel.type) in ('endOfSegment', 'endOfRoute')]
  devices = {str(event.initData.dongleId) for event in events if event.which() == 'initData' and event.valid}
  if not cps or len(endings) != 1 or len({car_params_digest(cp) for cp in cps}) != 1:
    raise ValueError('Original segment has no complete closing record or stable vehicle identity')
  if expected_device is not None and devices != {expected_device}:
    raise ValueError('Original segment belongs to another or an unrecorded device')
  cp = cps[0].as_builder()
  source_device = expected_device if expected_device is not None else next(iter(devices)) if len(devices) == 1 else ''
  bounds = [int(event.logMonoTime) for event in events if event.which() not in ('initData', 'carParams', 'sentinel') and event.logMonoTime > 0]
  if not bounds:
    raise ValueError('Original segment has no source-clock observations')
  first, last = min(bounds), max(bounds)
  exclusions, _, _ = recorded_driver_exclusions(events)
  # Each worker owns one source. Its neighbors are unobserved: discard uncertain
  # boundary windows rather than claiming no nearby manual input existed.
  clean = [event for event in events if event.which() != 'controlsState' or
           first + AFTER_NS <= int(event.logMonoTime) <= last - BEFORE_NS and
           not exclusions.overlaps(int(event.logMonoTime), int(event.logMonoTime))]
  groups = {}
  counts = {'trackingFrames': 0, 'controllerMetadataFrames': 0, 'routeScopeFrames': 0, 'omittedContexts': 0}
  vehicle, known_vin = summarize(clean, groups, counts)
  metrics = []
  for row in groups.values():
    metrics.append({key: row[key] for key in ('domain', 'speedRange', 'configurationHash', 'windows', 'samples')} | {
      'observedSeconds': row['seconds'], 'rmse': math.sqrt(row['squareError'] / row['samples']),
      'meanAbsoluteError': row['absoluteError'] / row['samples'], 'worstAbsoluteError': row['worstError'],
      'errorReversalHz': row['reversalSeconds'] / row['seconds'],
    })

  def walker(root, route, *, permitted, consume):
    if not permitted():
      raise InterruptedError('Full-route review paused')
    consume(number, events)
    return SelectedSourceReceipt(route_id, sha, ((source.name, sha, len(raw)),), car_params_digest(cp), first, last, 0, (), (number,), False)

  inputs = collect_route_inputs(source.parent, {'routeId': route_id, 'segments': []}, permitted=permitted,
                                walker=walker, isolate_segments=True)
  trim_unobserved_edges(inputs)
  proposals = []
  qualified_spans = []
  for context, windows in inputs.windows.items():
    metadata = inputs.context_metadata.get(context)
    if metadata is None or not windows:
      continue
    # Absolute values are original observations, never an assumed current map.
    actual = metadata.get('live_coordinates', ())
    prior = dict(zip(NAMES, actual, strict=True)) if len(actual) == len(NAMES) else dict.fromkeys(NAMES, 0.)
    configuration = _digest(('recorded-controller-v1', metadata))
    qualified_spans.extend((window.domain, window.points[0].mono_ns, window.points[-1].mono_ns) for window in windows)
    proposals.extend(_proposal_groups(windows, prior, configuration, analyze, cohort, permitted,
                                     metadata=metadata, baseline_known=bool(actual) or not metadata.get('route_revision')))
  # Missing old instrumentation does not erase measurements. These are clearly
  # marked observational intentions and still require fresh current native checks.
  for context, windows in inputs.raw_windows.items():
    windows = [window for window in windows if not any(
      domain == window.domain and max(first, window.points[0].mono_ns) <= min(last, window.points[-1].mono_ns)
      for domain, first, last in qualified_spans)]
    if not windows:
      continue
    configuration = _digest(('missing-original-controller-fields-v1', car_params_digest(cp), context))
    proposals.extend(_proposal_groups(windows, dict.fromkeys(NAMES, 0.), configuration, analyze, cohort, permitted,
                                     metadata=None, baseline_known=False))
  if not permitted() or _revision(source.stat()) != before:
    raise InterruptedError('Original segment changed or review paused')
  counts.update(inputs.counts)
  return {'version': 1, 'analysisRevision': ANALYSIS_REVISION, 'segment': number, 'routeId': route_id, 'sourceSha256': sha,
          'vehicleIdentity': _identity(cp, source_device), 'vehicles': [vehicle] if vehicle else [],
          'knownVinRecorded': known_vin, 'counts': counts, 'metrics': metrics,
          'proposalGroups': proposals, 'nativeQualified': False, 'actualApplicationProven': False,
          'originalRouteEndRecorded': endings == ['endOfRoute'],
          'boundaryPolicy': 'unknown_neighbors_1s_leading_350ms_trailing_whole_windows'}


def _proposal_groups(windows, previous, configuration, analyze, cohort, permitted, *, metadata, baseline_known):
  from openpilot.starpilot.flm.route_window_transport import pack_window
  by_cohort = {}
  for window in windows:
    name = cohort(window)
    if name is not None:
      by_cohort.setdefault(name, []).append(window)
  report = analyze(tuple(windows), previous, permitted=permitted)
  output = []
  for row in report['cohorts']:
    candidates = [row.get('proposal'), *row.get('coordinate_candidates', ())]
    changes = [{'parameter': candidate['parameter'].removeprefix('stable:'), 'delta': candidate['delta']}
               for candidate in candidates if candidate is not None]
    output.append({'configurationHash': configuration, 'cohort': row['cohort'],
                   'windows': len(by_cohort.get(row['cohort'], ())), 'proposals': changes,
                   'trackingWindows': [pack_window(window) for window in by_cohort.get(row['cohort'], ())],
                   'previousCoordinates': previous, 'sectionReason': row['reason'],
                   'metadataRecorded': metadata is not None, 'originalMetadata': metadata,
                   'originalBaselineKnown': baseline_known, 'nativeQualified': False})
  return output


def main():
  import ctypes
  import resource
  import signal
  parent = os.getppid()
  if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or parent == 1 or os.getppid() != parent:
    raise InterruptedError('Full-route worker parent is unavailable')
  os.nice(19)
  resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
  resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
  resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
  request = json.loads(sys.stdin.buffer.read(32768))
  result = analyze_segment(**request)
  raw = json.dumps(result, allow_nan=False).encode()
  if len(raw) > MAX_OUTPUT:
    raise ValueError('Segment report exceeds the output budget')
  sys.stdout.buffer.write(raw)


if __name__ == '__main__':
  main()
