"""Read the managed importer's durable progress without creating another worker."""

import hashlib
import json
import math
from pathlib import Path

from openpilot.starpilot.flm.historical_review import VERSION
from openpilot.starpilot.flm.offroad_cache import AssessmentCache
from openpilot.starpilot.galaxy.drive_history import SEGMENT_NAME
from openpilot.starpilot.saved_source import read_saved


def owner_context(root, params):
  return hashlib.sha256(repr((str(Path(root).resolve()), params.get_param_path() if params is not None else None)).encode()).hexdigest()


def local_signature(root, route):
  from openpilot.starpilot.flm.full_route_review import ANALYSIS_REVISION
  revisions = hashlib.sha256()
  for segment in route['segments']:
    revisions.update(repr((segment['number'], segment['segmentName'])).encode() + b'\n')
    folder = Path(root) / segment['segmentName']
    for name in ('rlog.zst', 'rlog.bz2', 'rlog.lock', 'qlog.lock'):
      try:
        info = (folder / name).lstat()
        revisions.update(repr((segment['number'], name, info.st_size, info.st_mtime_ns, info.st_ctime_ns)).encode() + b'\n')
      except FileNotFoundError:
        pass
  return hashlib.sha256((repr((VERSION, ANALYSIS_REVISION)) + revisions.hexdigest()).encode()).hexdigest()


def managed_local_progress(params, root, *, store=None):
  """Inert display data only; native qualification remains with the live owner.

  Only the exact log root/Params owner can replace the legacy latest-route fit.
  A stale/missing local row waits for the running owner's next inventory scan.
  Standalone callers with no matching managed owner retain the legacy fitter.
  """
  if store is None:
    from openpilot.starpilot.storage import starpilot_storage_root
    store = starpilot_storage_root() / 'galaxy' / 'route-reviews'
  store = Path(store)
  if not store.is_dir():
    return None
  try:
    value = json.loads(AssessmentCache(store)._read('reviews.json', 4_000_000))
    records = value['records']
    if (value.get('version') != VERSION or value.get('ownerContext') != owner_context(root, params) or
        type(records) is not dict or len(records) > 100):
      return None
    from opendbc.car.structs import car
    from openpilot.starpilot.flm.offroad_runtime import current_scope
    from openpilot.starpilot.schema_cache import inspect_cache
    saved, readable = read_saved(params, 'CarParamsPersistent', 1024 * 1024)
    payload = inspect_cache('CarParamsPersistent', saved).payload if readable and saved else None
    if payload is None:
      return None
    with car.CarParams.from_bytes(payload) as cp:
      current_scope(cp, params)  # Explicit OFF/manual/unsupported settings retain the normal refusal path.
    raw, okay = read_saved(params, 'CurrentRoute', 180)
    counter, count_ok = read_saved(params, 'RouteCount', 32)
    if not okay or not count_ok:
      return None
    route_id = raw.decode('ascii') if raw else None
    if route_id is None:
      if counter is None or not counter.isdigit() or not 0 < int(counter) <= 2**32:
        return None
      prefix = f'{(int(counter)-1) % 2**32:08x}--'
      choices = [row.get('routeId') for row in records.values() if type(row) is dict and row.get('source') == 'local'
                 and type(row.get('routeId')) is str and row['routeId'].startswith(prefix)]
      if len(choices) != 1:
        return None
      route_id = choices[0]
    from openpilot.starpilot.flm.offroad_route import LOCAL_ID
    if LOCAL_ID.fullmatch(route_id) is None:
      return None
    result = {'enabled': True, 'learning_mode': 'live', 'finishing': True, 'finishing_completed': False,
              'status': 'collecting', 'reason': 'waiting_for_resumable_local_route_inventory',
              'completed_route': route_id, 'managed_route_review': True, 'analysis_counts': {},
              'configuration_changed': False, 'actual_application_proven': False, 'road_improvement_confirmed': False,
              'requires_fresh_live_qualification': True, 'next_drive_candidate': None}
    row = records.get('local:' + route_id)
    if type(row) is not dict or row.get('routeId') != route_id or row.get('source') != 'local' or row.get('purpose') != 'tune':
      return result
    segments = []
    for folder in Path(root).iterdir():
      match = SEGMENT_NAME.fullmatch(folder.name)
      if (match is not None and match.group('route').replace('_', '|') == route_id and
          folder.is_dir() and not folder.is_symlink()):
        segments.append({'number': int(match.group('number')), 'segmentName': folder.name})
    segments.sort(key=lambda segment: segment['number'])
    if row.get('signature') != local_signature(root, {'segments': segments}):
      return result
    summary = row.get('summary') or {}
    if type(summary) is not dict:
      return None
    available, attempted = summary.get('availableSegments', 0), summary.get('attemptedSegments', 0)
    if type(available) is not int or type(attempted) is not int or not 0 <= attempted <= available:
      return None
    result['analysis_counts'] = {'original_segments_reviewed': attempted, 'original_segments_available': available}
    counts = summary.get('counts', {})
    if type(counts) is dict:
      for name in ('recorded_tracking', 'joined_tracking', 'original_control_metadata_rows', 'controller_fit_refusals'):
        count = counts.get(name)
        if type(count) is int and 0 <= count < 2**63:
          result['analysis_counts'][name] = count
    result['status'] = 'unavailable' if row.get('status') == 'failed' else 'fitting' if row.get('status') in ('queued', 'analyzing') else 'collecting'
    result['reason'] = ('resumable_local_route_analysis' if result['status'] == 'fitting' else
                        'resumable_local_route_review_unavailable' if result['status'] == 'unavailable' else
                        'parked_live_fit_needs_more_consistent_evidence')
    complete = row.get('status') == 'ready' and summary.get('allSegmentsAttempted') is True
    result['finishing_completed'] = complete
    if complete:
      selected = summary.get('tuning', {}).get('selected')
      if type(selected) is dict:
        from openpilot.starpilot.flm.canonical_surface_adapter import NAMES
        name, delta = selected.get('parameter'), selected.get('delta')
        if name in NAMES and type(delta) in (int, float) and math.isfinite(delta) and 0 < abs(delta) <= .005:
          result['next_drive_candidate'] = {'parameter': name, 'delta': delta}
          result['reason'] = 'parked_candidate_waiting_for_fresh_live_checks'
      source = summary.get('sourceSha256')
      if type(source) is str and len(source) == 64 and not source.strip('0123456789abcdef'):
        result['source_sha256'] = source
    return result
  except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError):
    return None
