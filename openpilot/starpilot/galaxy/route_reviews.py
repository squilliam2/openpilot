"""Resumable original-route reviews and intents for freshly qualified tuning."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from openpilot.starpilot.flm.historical_review import VERSION, selected_rows
from openpilot.starpilot.flm.offroad_cache import AssessmentCache
from openpilot.starpilot.galaxy.connect_route_review import parse_route, fetch, ReviewUnavailable, RetryableReview
from openpilot.starpilot.galaxy.drive_history import DriveHistory, MAX_ROOT_ENTRIES
from openpilot.starpilot.galaxy.drive_stats import _route_signature

MAX_RECORDS = 100
MAX_QUEUE = 8


class RouteReviews:
  def __init__(self, root, store, *, permitted, device_id=lambda: '', analyzer=None, downloader=fetch, clock=None,
               params=None, stream=None):
    self.root = Path(root)
    self.cache = AssessmentCache(store)
    self.permitted, self.device_id = permitted, device_id
    self.analyzer, self.downloader = analyzer, downloader
    self.clock = clock if clock is not None else lambda: datetime.now(UTC).timestamp()
    self.history = DriveHistory(self.root, max_segments_per_route=512, max_segments=MAX_ROOT_ENTRIES)
    self.lock = threading.RLock()
    self.stop = threading.Event()
    self.wake = threading.Event()
    self.thread = self.child = None
    self.records, self.local = {}, {}
    self.scan_incomplete = False
    self.running = None
    self.last_scan = 0.
    self.params = params
    self.stream = stream
    if stream is None and analyzer is None and downloader is fetch:
      from openpilot.starpilot.galaxy.stream_route_review import StreamRouteReview
      self.stream = StreamRouteReview(self.cache.directory / 'full-routes', permitted=self._allowed,
                                      log_root=self.root, device_id=self.device_id)
    try:
      value = json.loads(self.cache._read('reviews.json', 4_000_000))
      records = value['records']
      if (value['version'] != VERSION or type(records) is not dict or len(records) > MAX_RECORDS or
          any(type(k) is not str or type(r) is not dict or r.get('source') not in ('local', 'connect') or
              type(r.get('routeId')) is not str or len(r['routeId']) > 180 or
              r.get('status') not in ('queued', 'analyzing', 'ready', 'failed') for k, r in records.items())):
        raise ValueError('Invalid review cache')
      self.records = records
      for row in self.records.values():
        if row['status'] == 'analyzing':
          row['status'] = 'queued'
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
      pass

  def _allowed(self):
    try:
      return not self.stop.is_set() and bool(self.permitted())
    except Exception:
      return False

  def _save(self):
    from openpilot.starpilot.galaxy.route_review_progress import owner_context
    raw = json.dumps({'version': VERSION, 'ownerContext': owner_context(self.root, self.params),
                      'records': self.records}, allow_nan=False, separators=(',', ':')).encode()
    self.cache._write('reviews.json', raw, maximum=4_000_000)

  def scan(self):
    inventory = ({'routes': list(self.stream.local_routes().values()), 'scanIncomplete': False}
                 if self.stream is not None else self.history.snapshot())
    local = {r['routeId']: r for r in inventory['routes'] if self.stream is not None or any(s['files'].get('rlog') for s in r['segments'])}
    with self.lock:
      self.local = local
      self.scan_incomplete = inventory['scanIncomplete']
      budget = max(0, MAX_RECORDS - sum(r['source'] == 'connect' for r in self.records.values()))
      wanted = dict(sorted(local.items(), reverse=True)[:budget])
      if len(wanted) < len(local):
        self.scan_incomplete = True
      for route_id, route in wanted.items():
        if self.stream is not None:
          from openpilot.starpilot.galaxy.route_review_progress import local_signature
          signature = local_signature(self.root, route)
        else:
          signature, _ = _route_signature(self.root, route)
          signature = hashlib.sha256((str(VERSION) + signature).encode()).hexdigest()
        key = 'local:' + route_id
        old = self.records.get(key)
        if old is not None and old.get('signature') == signature:
          continue
        if old is None and len(self.records) >= MAX_RECORDS:
          obsolete = [k for k, r in self.records.items() if r['source'] == 'local' and r['routeId'] not in wanted
                      and k != self.running and (r['status'] in ('ready', 'failed') or
                                                 r['status'] == 'queued' and not r.get('requested'))]
          if not obsolete:
            self.scan_incomplete = True
            continue
          del self.records[min(obsolete, key=lambda k: self.records[k]['updatedAt'])]
        if self.stream is not None and self.params is not None:
          from openpilot.starpilot.flm.route_tuning_import import retire_unoffered
          retire_unoffered(self.params, route_id=route_id, authorized=self._allowed)
        self.records[key] = {'source': 'local', 'routeId': route_id, 'status': 'queued', 'signature': signature,
                             'purpose': (old or {}).get('purpose', 'tune' if self.stream is not None and self.params is not None else 'review'),
                             'reason': 'Waiting to resume automatic parked local review', 'updatedAt': self.clock(),
                             'lastAttemptAt': (old or {}).get('lastAttemptAt', 0),
                             'lastScheduled': (old or {}).get('lastScheduled', 0), 'summary': (old or {}).get('summary')}
      self.last_scan = time.monotonic()
      self._save()

  def snapshot(self):
    imports = {}
    if self.params is not None:
      from openpilot.starpilot.flm.route_tuning_import import snapshot
      try:
        imports = {row['intentId']: row for row in snapshot(self.params)}
      except (OSError, ValueError):
        pass
    def public(row):
      row = dict(row)
      summary = row.get('summary')
      if summary is not None and summary.get('tuning', {}).get('intentId') in imports:
        row['summary'] = dict(summary, application=imports[summary['tuning']['intentId']])
      return row
    with self.lock:
      return {'version': VERSION, 'parked': self._allowed(), 'running': self.running,
              'scanIncomplete': self.scan_incomplete,
              'localRoutes': [{'routeId': name, 'segments': len(route['segments'])}
                              for name, route in sorted(self.local.items(), reverse=True)[:MAX_RECORDS]],
              'reviews': [public(row) for row in sorted(self.records.values(), key=lambda r: r['updatedAt'], reverse=True)],
              'scope': 'historical_tracking_and_qualified_tuning' if self.stream is not None else 'historical_tracking_only',
              'qualifiedTuningEnabled': self.stream is not None and self.params is not None,
              'automaticApplicationAllowed': False}

  def request(self, source, route_id, *, authorized, purpose='review'):
    if not authorized():
      raise PermissionError('Galaxy session expired')
    if source not in ('local', 'connect') or type(route_id) is not str or purpose not in ('review', 'tune'):
      raise ValueError('Choose an existing local or Connect route')
    if source == 'connect':
      route_id = parse_route(route_id, self.device_id())
    else:
      self.scan()
      if route_id not in self.local:
        raise ValueError('That local recording is no longer available')
    with self.lock:
      if not authorized():
        raise PermissionError('Galaxy session expired')
      key = source + ':' + route_id
      if key == self.running:
        return self.snapshot()
      if sum(r['status'] == 'queued' and bool(r.get('requested')) for r in self.records.values()) >= MAX_QUEUE:
        raise ValueError('The requested-review queue is full; wait for a review to finish')
      old = self.records.get(key)
      if old is None and len(self.records) >= MAX_RECORDS:
        removable = [k for k, r in self.records.items() if r['status'] in ('ready', 'failed') and k != self.running]
        if not removable:
          raise ValueError('The historical review library is full')
        del self.records[min(removable, key=lambda k: self.records[k]['updatedAt'])]
      row = dict(old or {}, source=source, routeId=route_id, status='queued', requested=True,
                 purpose=purpose,
                 reason='Queued; Connect downloads run only while parked' if source == 'connect' else 'Queued for historical review',
                 updatedAt=self.clock())
      row.setdefault('summary', None)
      restart = (old is None or old.get('status') == 'ready' or
                 old.get('status') == 'failed' and str(old.get('reason', '')).startswith('Original route inventory changed;'))
      if self.stream is not None and restart:
        if self.params is not None:
          from openpilot.starpilot.flm.route_tuning_import import retire_unoffered
          retire_unoffered(self.params, route_id=route_id, authorized=authorized)
        # The UI may queue a new job while another route owns the importer
        # lease. Only a finished/changed-inventory rerun needs a later reset.
        row['restartRequired'] = old is not None
      self.records[key] = row
      self._save()
      self.wake.set()
    self.start()
    return self.snapshot()

  def _full_review(self, row, *, permitted):
    from openpilot.starpilot.flm.route_tuning_consensus import RouteConsensus
    route_id = row['routeId']
    if row.get('restartRequired'):
      if not permitted():
        raise InterruptedError('Full-route restart paused')
      self.stream.reset(route_id)
      with self.lock:
        self.records[row['source'] + ':' + route_id]['restartRequired'] = False
        self._save()
    def progress(value):
      if not permitted():
        raise InterruptedError('Full-route review paused')
      with self.lock:
        key = row['source'] + ':' + route_id
        self.records[key].update(summary=value, updatedAt=self.clock(),
          reason=f"Reviewed {value['attemptedSegments']} of {value['availableSegments']} segments; progress saved")
        self._save()
    result = self.stream.run(route_id, purpose=row.get('purpose', 'review'),
                             route=None if row['source'] == 'connect' else self.local[route_id], on_progress=progress)
    if not permitted():
      raise InterruptedError('Full-route review paused')
    if not result['allSegmentsAttempted']:
      return result, 'queued', f"Reviewed {result['attemptedSegments']} of {result['availableSegments']} segments; parked processing will resume"
    if row.get('purpose') != 'tune':
      return result, 'ready', 'All available sections attempted; tracking reviewed'
    from openpilot.starpilot.flm.full_route_review import ANALYSIS_REVISION
    comparison_id = hashlib.sha256((str(ANALYSIS_REVISION) + route_id + result['sourceSha256']).encode()).hexdigest()
    consensus_path = self.stream.store / (comparison_id + '-comparison.sqlite3')
    comparison = RouteConsensus(consensus_path, route_id)
    for segment in self.stream.iter_segments(route_id):
      if not permitted():
        raise InterruptedError('Full-route comparison paused')
      comparison.add(segment)
    mode, convergence = 'live', 'standard'
    if self.params is not None:
      from openpilot.starpilot.flm.learning_mode import selected_mode
      mode = selected_mode(self.params)
      from openpilot.starpilot.schema_cache import inspect_cache
      from openpilot.starpilot.saved_source import read_saved
      from opendbc.car.structs import car
      raw, okay = read_saved(self.params, 'CarParamsPersistent', 1024 * 1024)
      payload = inspect_cache('CarParamsPersistent', raw).payload if okay and raw else None
      if payload is not None:
        from openpilot.starpilot.lateral.learning_preferences import read_convergence_mode
        with car.CarParams.from_bytes(payload) as cp:
          convergence = read_convergence_mode(self.params, cp)
    proof = comparison.summary(convergence=convergence, mode=mode, permitted=permitted)
    if not permitted():
      raise InterruptedError('Full-route comparison paused')
    result['tuning'] = proof
    if proof['selected'] is None:
      return result, 'ready', 'Full-route comparison finished; more consistent independent evidence is needed'
    if self.params is None:
      return result, 'ready', 'Full-route comparison finished; current vehicle is needed before queuing a trial'
    from openpilot.starpilot.flm.route_tuning_import import enqueue
    intent = enqueue(self.params, route_id=route_id, source_sha256=proof['sourceSha256'], proposal=proof['selected'],
                     authorized=permitted)
    result['tuning']['intentId'] = intent
    return result, 'ready', 'Full-route candidate queued for fresh controller qualification and automatic next-drive trial'

  def _analyze(self, root, route, *, permitted, expected_device=None):
    if self.analyzer is not None:
      return self.analyzer(root, route, permitted=permitted, expected_device=expected_device)
    selected = [{'number': r['number'], 'segmentName': r['segmentName'],
                 'files': {name: r['files'].get(name, False) for name in ('rlog', 'qlog')}} for r in selected_rows(route['segments'])]
    request = json.dumps({'root': str(root), 'route': {'routeId': route['routeId'], 'segments': selected},
                          'expectedDevice': expected_device}).encode()
    if len(request) > 128 * 1024:
      raise ReviewUnavailable('Historical review selection exceeds its input bound')
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
      child = subprocess.Popen([sys.executable, '-m', 'openpilot.starpilot.flm.historical_review'],
                               stdin=subprocess.PIPE, stdout=output, stderr=errors)
      with self.lock:
        self.child = child
      try:
        child.stdin.write(request)
        child.stdin.close()
        deadline = time.monotonic() + 240
        while child.poll() is None:
          if not permitted():
            raise InterruptedError('Historical review paused when the vehicle started')
          if time.monotonic() >= deadline:
            raise ReviewUnavailable('Historical review reached its bounded processing deadline')
          self.stop.wait(.1)
        if not permitted():
          raise InterruptedError('Historical review cancelled')
        if child.returncode:
          raise ReviewUnavailable('Original recording could not be reviewed; no tune was changed')
        output.seek(0)
        raw = output.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
          raise ReviewUnavailable('Historical review output exceeds its bound')
        result = json.loads(raw)
        result['availableSegments'] = len(route['segments'])
        result['wholeRouteReviewed'] = len(result['reviewedSegments']) == len(route['segments'])
        return result
      finally:
        if child.poll() is None:
          child.terminate()
          try:
            child.wait(timeout=2)
          except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
        with self.lock:
          self.child = None

  def run_once(self):
    if not self._allowed():
      return False
    if not self.last_scan or time.monotonic() - self.last_scan >= 60:
      self.scan()
    with self.lock:
      candidates = [(k, r) for k, r in self.records.items() if r['status'] == 'queued' and (r['source'] == 'connect' or r['routeId'] in self.local)]
      if not candidates:
        return False
      # Local drives start automatically; round-robin batches prevent a long
      # requested Connect job from starving them (or the reverse).
      def scheduled(record):
        value = record.get('lastScheduled', 0)
        return value if type(value) is int and 0 <= value < 2**53 else 0
      key, row = min(candidates, key=lambda pair: (
        not (pair[1]['source'] == 'local' and not scheduled(pair[1])),
        scheduled(pair[1]), not pair[1].get('requested', False), -pair[1]['updatedAt'], pair[0]))
      row = dict(row)
      self.running = key
      self.records[key]['status'] = 'analyzing'
      self.records[key]['lastAttemptAt'] = self.clock()
      self.records[key]['lastScheduled'] = min(2**53 - 1, max(map(scheduled, self.records.values()), default=0) + 1)
      self._save()
    deadline = time.monotonic() + 600
    def permitted():
      return self._allowed() and time.monotonic() < deadline
    try:
      if self.stream is not None:
        result, status, reason = self._full_review(row, permitted=permitted)
        with self.lock:
          self.records[key].update(status=status, summary=result, reason=reason, updatedAt=self.clock())
        return True
      if row['source'] == 'connect':
        # TemporaryDirectory is inside this private cache, never the logger root.
        with tempfile.TemporaryDirectory(prefix='connect-review-', dir=self.cache.directory) as folder:
          route, available = self.downloader(row['routeId'], folder, permitted=permitted)
          result = self._analyze(Path(folder), route, permitted=permitted, expected_device=row['routeId'].split('|')[0])
          result['availableSegments'] = available
          result['wholeRouteReviewed'] = len(result['reviewedSegments']) == available
      else:
        route = self.local[row['routeId']]
        before = _route_signature(self.root, route)
        result = self._analyze(self.root, route, permitted=permitted)
        if _route_signature(self.root, route) != before:
          raise InterruptedError('Historical source changed; waiting for a fresh inventory')
      if (not permitted() or result.get('routeId') != row['routeId'] or result.get('analysisOnly') is not True or
          result.get('automaticApplicationAllowed') is not False or result.get('tuneChanged') is not False):
        raise InterruptedError('Historical review did not return an observational result')
      with self.lock:
        self.records[key].update(status='ready', summary=result, reason='Recorded tracking reviewed; tune unchanged', updatedAt=self.clock())
    except InterruptedError:
      with self.lock:
        self.records[key].update(status='queued', reason='Waiting to resume parked historical review')
    except RetryableReview:
      with self.lock:
        self.records[key].update(status='queued', reason='Transfer paused; saved progress will resume when the source is available', updatedAt=self.clock())
    except ReviewUnavailable as error:
      with self.lock:
        self.records[key].update(status='failed', reason=str(error)[:200], updatedAt=self.clock())
    except Exception:
      # Never place signed download URLs, credentials or subprocess stderr in a report.
      with self.lock:
        self.records[key].update(status='failed', reason='Review unavailable; check original rlogs, Wi-Fi and Connect access', updatedAt=self.clock())
    finally:
      with self.lock:
        self.running = None
        self._save()
    return True

  def start(self):
    with self.lock:
      if self.thread is not None or self.stop.is_set():
        return
      self.thread = threading.Thread(target=self._run, name='galaxy-route-reviews', daemon=True)
      self.thread.start()

  def _run(self):
    while not self.stop.is_set():
      try:
        worked = self.run_once()
      except Exception:
        worked = False
      self.wake.wait(1 if worked else 10)
      self.wake.clear()

  def stop_child(self):
    self.stop.set()
    self.wake.set()
    with self.lock:
      child = self.child
    if child is not None and child.poll() is None:
      child.terminate()

  def close(self):
    self.stop_child()
    if self.thread is not None and self.thread is not threading.current_thread():
      self.thread.join(timeout=6)
