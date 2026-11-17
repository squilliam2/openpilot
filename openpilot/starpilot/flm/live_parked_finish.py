"""Finish live-drive fitting while parked; hand off intent, never permission.

Parked candidates guide a NEW live experiment with fresh native sensitivity,
baseline and future outcomes. They are not confirmed history or a second map.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from openpilot.starpilot.flm.canonical_surface_adapter import NAMES, GROUPS
from openpilot.starpilot.flm.offroad_cache import AssessmentCache, directory_for_params
from openpilot.starpilot.flm.offroad_store import RouteScope

VERSION = 2
MAX_BYTES = 32768


def cache_directory(params):
  base = directory_for_params(params)
  return base.with_name(base.name + '-live-finish')


@dataclass(frozen=True)
class ParkedCandidate:
  scope: RouteScope
  previous: tuple
  name: str
  delta: float
  source_sha256: str

  def validate(self):
    self.scope.validate()
    from openpilot.starpilot.flm.live_trial_contract import full_coordinates
    full_coordinates(self.previous, self.scope.baseline_revision)
    if (self.name not in NAMES or type(self.delta) not in (int, float) or not 0 < abs(self.delta) <= .005 or
        type(self.source_sha256) is not str or len(self.source_sha256) != 64 or self.source_sha256.strip('0123456789abcdef')):
      raise ValueError('Bounded, source-bound parked candidate required')
    return self

  def matches(self, binding, domain, accepted):
    return bool(
      (binding.cp_sha256, binding.controller_revision, binding.baseline_revision, binding.persistent_source_epoch, binding.reset_generation)
      == (self.scope.cp_sha256, self.scope.controller_revision, self.scope.baseline_revision,
          self.scope.persistent_source_epoch, self.scope.reset_generation)
      and self.scope.controller_domain == domain and dict(self.previous) == accepted
    )

  @property
  def group(self):
    return next(group for group, names in GROUPS.items() if self.name in names)

  def document(self):
    self.validate()
    return {'version': VERSION, 'scope': self.scope.document(), 'previous': self.previous, 'name': self.name,
            'delta': self.delta, 'source_sha256': self.source_sha256, 'requires_fresh_live_qualification': True}


def load_candidate(params):
  try:
    raw = AssessmentCache(cache_directory(params))._read('candidate.json', MAX_BYTES)
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {'version', 'scope', 'previous', 'name', 'delta', 'source_sha256',
                                               'requires_fresh_live_qualification'} or
        value['version'] != VERSION or value['requires_fresh_live_qualification'] is not True):
      return None
    return ParkedCandidate(RouteScope(**value['scope']), tuple(tuple(p) for p in value['previous']),
                           value['name'], value['delta'], value['source_sha256']).validate()
  except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, RecursionError):
    return None


def fit_candidate(inputs, scope, previous, *, permitted, counts=None):
  """Input-ranked, disjoint-window fits; no native/apply claim is manufactured."""
  from openpilot.starpilot.flm.offroad_analysis import analyze, _spread, cohort

  eligible = []
  counts = {} if counts is None else counts
  counts.update(parked_matching_scope_windows=0, parked_matching_profile_windows=0)
  for context, windows in inputs.windows.items():
    metadata = inputs.context_metadata[context]
    if (metadata['cp_sha256'] != scope.cp_sha256 or metadata['controller_revision'] != scope.controller_revision or
        metadata['baseline_revision'] != scope.baseline_revision or metadata['controller_mode'] != scope.controller_mode or
        metadata.get('route_revision') or metadata.get('unmodeled_effect')):
      continue  # Parked-relative effects are distinct from canonical live maps.
    counts['parked_matching_scope_windows'] += len(windows)
    actual = metadata.get('live_coordinates', ())
    actual = tuple(zip(NAMES, actual, strict=True)) if actual else tuple((n, 0.) for n in NAMES)
    if actual != previous:
      continue  # A changing trial/profile never gets pooled into one baseline.
    counts['parked_matching_profile_windows'] += len(windows)
    groups = {}
    for window in windows:
      key = cohort(window)
      if key is not None:
        groups.setdefault(key, []).append(window)
    for key, rows in groups.items():
      ordered = sorted(rows, key=lambda w: (w.points[0].segment, w.points[0].mono_ns))
      eligible.append((len(ordered), context, key, _spread(ordered, 24)))
  # Freeze coverage order before reading any fitted error or validation result.
  eligible.sort(key=lambda row: (-row[0], row[1], row[2]))
  for _, _, key, windows in eligible[:24]:
    if not permitted():
      raise ValueError('Parked finishing cancelled')
    report = analyze(tuple(windows), dict(previous), permitted=permitted)
    row = next((c for c in report['cohorts'] if c['cohort'] == key), None)
    proposals = [] if row is None else [row.get('proposal'), *row.get('coordinate_candidates', ())]
    proposal = next((p for p in proposals if p is not None and p['parameter'] in NAMES), None)
    if proposal is not None:
      delta = max(-.005, min(.005, proposal['delta']))
      return ParkedCandidate(scope, previous, proposal['parameter'], delta, inputs.receipt.source_sha256).validate()
  return None


def trim_unobserved_edges(inputs):
  """Missing adjacent logs cannot certify absence of nearby driver input."""
  from openpilot.starpilot.flm.driver_exclusions import BEFORE_NS, AFTER_NS
  bounds = inputs.segment_bounds
  removed = set()
  for table in (inputs.windows, inputs.stable_windows, inputs.baseline_tracking, inputs.raw_windows):
    for context in tuple(table):
      kept = []
      for window in table[context]:
        number = window.points[0].segment
        first, last = bounds[number]
        previous, following = bounds.get(number - 1), bounds.get(number + 1)
        observed_before = previous is not None and abs(first - previous[1]) <= 50_000_000
        observed_after = following is not None and abs(following[0] - last) <= 50_000_000
        if ((not observed_before and window.points[0].mono_ns < first + AFTER_NS) or
            (not observed_after and window.points[-1].mono_ns > last - BEFORE_NS)):
          removed.add(window.identity)
        else:
          kept.append(window)
      if kept:
        table[context] = kept
      else:
        del table[context]
  inputs.counts['unobserved_boundary_excluded_windows'] = len(removed)


class LiveParkedFinisher:
  def __init__(self, params, root, cache=None, *, permitted, on_analysis=None):
    self.params, self.root = params, Path(root)
    self.cache = AssessmentCache(cache_directory(params))
    self.permitted, self.on_analysis = permitted, on_analysis
    here = Path(__file__).parent
    self.implementation = hashlib.sha256(b''.join((here / name).read_bytes() for name in (
      'live_parked_finish.py', 'offroad_processor.py', 'offroad_evidence.py', 'driver_exclusions.py',
      'offroad_analysis.py', 'offroad_tracking.py', 'offroad_partial.py', 'offroad_route.py',
      'offroad_prior_inputs.py', 'log_decode.py', 'offroad_control_metadata.py',
      '../galaxy/drive_analysis.py', '../galaxy/drive_history.py',
    ))).hexdigest()

  def run_once(self):
    if not self.permitted():
      raise ValueError('Live-drive finishing waits until parked')
    from openpilot.starpilot.flm.learning_mode import selected_mode
    from openpilot.starpilot.galaxy.route_review_progress import managed_local_progress
    managed = managed_local_progress(self.params, self.root) if selected_mode(self.params) == 'live' else None
    if managed is not None:
      if not self.permitted() or selected_mode(self.params) != 'live':
        raise ValueError('Parked route progress was interrupted')
      if managed['finishing_completed']:
        # The durable full-route import intent owns the new candidate. An older
        # legacy fitter suggestion must not survive a no-consensus replacement.
        self.cache._write('candidate.json', json.dumps({'version': VERSION, 'candidate': None}).encode(), maximum=MAX_BYTES)
      return managed
    if self.on_analysis is not None:
      self.on_analysis()
    request = {'paramsRoot': str(Path(self.params.get_param_path()).parent), 'namespace': self.params.get_param_path(), 'root': str(self.root)}
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
      child = subprocess.Popen([sys.executable, '-m', 'openpilot.starpilot.flm.live_parked_finish'],
                               stdin=subprocess.PIPE, stdout=output, stderr=errors,
                               env=dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1'))
      try:
        if child.stdin is None:
          raise ValueError('Parked fitter input pipe is unavailable')
        child.stdin.write(json.dumps(request).encode())
        child.stdin.close()
        deadline = time.monotonic() + 330
        while child.poll() is None:
          if not self.permitted() or time.monotonic() >= deadline:
            raise ValueError('Parked finishing paused or reached its processing deadline')
          time.sleep(.1)
        if not self.permitted():
          raise ValueError('Parked finishing was interrupted')
        if child.returncode:
          raise ValueError('Live-drive finishing could not decode or fit the original recording')
        output.seek(0)
        raw = output.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
          raise ValueError('Parked finishing output exceeds its bound')
        result = json.loads(raw)
        if result.get('actual_application_proven') is not False or result.get('road_improvement_confirmed') is not False:
          raise ValueError('A parked fit cannot claim live application or confirmation')
        return result
      finally:
        if child.poll() is None:
          child.terminate()
          try:
            child.wait(timeout=2)
          except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)

  def _run_once(self):
    from openpilot.starpilot.flm.offroad_processor import collect_latest_steering_inputs, collect_route_inputs
    from openpilot.starpilot.flm.offroad_route import latest_route, source_revisions, logger_marker, PartialRouteReceipt, RouteInputCancelled
    from openpilot.starpilot.flm.offroad_runtime import current_scope
    from openpilot.starpilot.flm.live_trial_store import load_confirmed_history, KEY as LIVE_KEY, MAX_BYTES as LIVE_BYTES
    from openpilot.starpilot.flm.learning_mode import selected_mode
    from openpilot.starpilot.saved_source import read_saved
    from openpilot.starpilot.schema_cache import inspect_cache
    from opendbc.car import structs

    if not self.permitted() or selected_mode(self.params) != 'live':
      raise ValueError('Live-drive finishing waits until parked with live mode selected')
    # Retain the exact logger-owned identity even when an unavailable tail
    # removes the complete route from inventory. Never select by file mtime.
    route = latest_route(self.params, self.root, permitted=self.permitted, inventory_only=True)
    original_marker = logger_marker(self.params)
    revisions = source_revisions(self.root, route)
    cp_raw, readable = read_saved(self.params, 'CarParamsPersistent', 1024 * 1024)
    cp_bytes = inspect_cache('CarParamsPersistent', cp_raw).payload if readable and cp_raw else None
    if cp_bytes is None:
      raise ValueError('Current saved vehicle is unavailable for parked finishing')
    with structs.CarParams.from_bytes(cp_bytes) as saved_cp:
      cp = saved_cp.as_builder()
    scope = current_scope(cp, self.params)
    history = read_saved(self.params, LIVE_KEY, LIVE_BYTES)
    if not history[1]:
      raise ValueError('Confirmed live history is unreadable')
    # A checkpoint is only a display/skip cache; no live clock or permission.
    signature = hashlib.sha256(repr((VERSION, self.implementation, route, revisions, original_marker, scope.key(), history)).encode()).hexdigest()
    def unchanged():
      return (self.permitted() and selected_mode(self.params) == 'live' and logger_marker(self.params) == original_marker and
              read_saved(self.params, 'CarParamsPersistent', 1024 * 1024) == (cp_raw, readable) and
              read_saved(self.params, LIVE_KEY, LIVE_BYTES) == history and
              current_scope(cp, self.params) == scope and source_revisions(self.root, route) == revisions)
    try:
      old = json.loads(self.cache._read('checkpoint.json', MAX_BYTES))
      if old.get('signature') == signature and old.get('version') == VERSION and unchanged():
        return old['result']
    except (OSError, ValueError, TypeError, KeyError):
      pass
    if self.on_analysis is not None:
      self.on_analysis()
    started = time.monotonic()
    def allowed():
      return self.permitted() and time.monotonic() - started <= 300
    recovery_inventory = None
    def recover():
      nonlocal recovery_inventory
      from openpilot.starpilot.flm.offroad_partial import partial_inventory, walk_partial_route
      recovery_inventory = partial_inventory(self.root, route['routeId'], permitted=allowed)
      def walk(root, selected, *, permitted, consume):
        return walk_partial_route(root, selected, permitted=permitted, consume=consume, selection_limit=14)
      result = collect_route_inputs(self.root, recovery_inventory, permitted=allowed, walker=walk, isolate_segments=True)
      trim_unobserved_edges(result)
      return result
    try:
      if route.get('selection_error') or len(route['segments']) > 30:
        inputs = recover()
      else:
        try:
          _, inputs = collect_latest_steering_inputs(self.params, self.root, permitted=allowed, selected_route=route)
        except RouteInputCancelled:
          raise
        except (OSError, ValueError):
          if not allowed():
            raise
          inputs = recover()
    except RouteInputCancelled:
      raise
    except (OSError, ValueError) as error:
      if not allowed() or not unchanged():
        raise ValueError('Parked finishing interrupted; original recording will be retried') from error
      result = dict(self.record_failure(error), road_improvement_confirmed=False)
      self.cache._write('checkpoint.json', json.dumps({'version': VERSION, 'signature': signature, 'result': result}).encode(), maximum=MAX_BYTES)
      return result  # Repair/closure/settings/code changes invalidate this memo.
    if current_scope(inputs.state['cp'], self.params) != scope:
      raise ValueError('Recorded drive belongs to a different current vehicle/settings scope')
    if recovery_inventory is not None:
      from openpilot.starpilot.flm.offroad_partial import partial_inventory
      if partial_inventory(self.root, route['routeId'], permitted=allowed) != recovery_inventory:
        raise ValueError('Recovered recording changed during analysis')
    previous = tuple((n, 0.) for n in NAMES)
    for prior in load_confirmed_history(self.params):
      if ParkedCandidate(scope, previous, NAMES[0], .005, inputs.receipt.source_sha256).matches(
          prior.binding, scope.controller_domain, dict(previous)):
        previous = prior.target
        break
    fit_counts = {}
    candidate = fit_candidate(inputs, scope, previous, permitted=allowed, counts=fit_counts)
    if not allowed() or not unchanged():
      raise ValueError('Vehicle, settings or recording changed during parked finishing')
    # Replacing an obsolete candidate with an explicit empty result prevents an
    # older rejected direction from surviving a newer completed-drive review.
    raw = json.dumps(candidate.document() if candidate else {'version': VERSION, 'candidate': None}, allow_nan=False).encode()
    self.cache._write('candidate.json', raw, maximum=MAX_BYTES)
    counts = dict(inputs.counts, **fit_counts, qualified_whole_windows=sum(map(len, inputs.windows.values())),
                  stable_qualified_whole_windows=sum(map(len, inputs.stable_windows.values())))
    reason = 'parked_live_fit_needs_more_consistent_evidence'
    if not fit_counts['parked_matching_scope_windows']:
      reason = 'parked_live_current_controller_not_recorded'
    elif not fit_counts['parked_matching_profile_windows']:
      reason = 'parked_live_current_accepted_profile_not_recorded'
    result = {'enabled': True, 'learning_mode': 'live', 'finishing': True, 'finishing_completed': True, 'status': 'collecting',
              'reason': 'parked_candidate_waiting_for_fresh_live_checks' if candidate else reason,
              'completed_route': inputs.receipt.route_id, 'vehicle': str(inputs.state['cp'].carFingerprint),
              'analysis_counts': counts, 'configuration_changed': False,
              'actual_application_proven': False, 'road_improvement_confirmed': False,
              'source_sha256': inputs.receipt.source_sha256,
              'next_drive_candidate': None if candidate is None else {'parameter': candidate.name, 'delta': candidate.delta},
              'requires_fresh_live_qualification': True}
    if type(inputs.receipt) is PartialRouteReceipt:
      result.update(partial_drive=True,
                    partial_segments=[len(inputs.receipt.decoded_segments), len(inputs.receipt.segments)],
                    omitted_segments=[list(row) for row in inputs.receipt.omitted_segments],
                    recorded_cutoff_ns=inputs.receipt.original_end_ns, route_completion_proven=False)
    if not allowed() or not unchanged():
      raise ValueError('Parked finishing was interrupted; no accepted tune changed')
    self.cache._write('checkpoint.json', json.dumps({'version': VERSION, 'signature': signature, 'result': result}, allow_nan=False).encode(),
                      maximum=MAX_BYTES)
    return result

  def record_failure(self, error):
    return {'status': 'unavailable', 'reason': str(error)[:200], 'learning_mode': 'live', 'finishing': True,
            'configuration_changed': False, 'actual_application_proven': False}


def main():
  import ctypes
  import resource
  import signal
  from openpilot.common.params import Params
  from openpilot.starpilot.flm.learner_cpu import configure_cpu
  parent = os.getppid()
  if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or parent == 1 or os.getppid() != parent:
    raise ValueError('Parked fitter parent is unavailable')
  os.nice(19)
  configure_cpu(numerical=True)
  resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
  resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
  resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
  request = json.loads(sys.stdin.buffer.read(8192))
  params = Params(request['paramsRoot'])
  if params.get_param_path() != request['namespace']:
    raise ValueError('Original Params namespace changed')
  worker = LiveParkedFinisher(params, request['root'], permitted=lambda: params.get_bool('IsOffroad'))
  try:
    result = worker._run_once()
  except (OSError, RuntimeError, ValueError) as error:
    result = worker.record_failure(error)
    result['road_improvement_confirmed'] = False
  sys.stdout.write(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
  main()
