"""Parked historical reviews, isolated from automatic tuning and logger identity."""

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
from openpilot.starpilot.galaxy.connect_route_review import parse_route, fetch, ReviewUnavailable
from openpilot.starpilot.galaxy.drive_history import DriveHistory, MAX_ROOT_ENTRIES
from openpilot.starpilot.galaxy.drive_stats import _route_signature

MAX_RECORDS = 100
MAX_QUEUE = 8


class RouteReviews:
  def __init__(self, root, store, *, permitted, device_id=lambda: '', analyzer=None, downloader=fetch, clock=None):
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
    raw = json.dumps({'version': VERSION, 'records': self.records}, allow_nan=False, separators=(',', ':')).encode()
    self.cache._write('reviews.json', raw, maximum=4_000_000)

  def scan(self):
    inventory = self.history.snapshot()
    local = {r['routeId']: r for r in inventory['routes'] if any(s['files'].get('rlog') for s in r['segments'])}
    with self.lock:
      self.local = local
      self.scan_incomplete = inventory['scanIncomplete']
      budget = max(0, MAX_RECORDS - sum(r['source'] == 'connect' for r in self.records.values()))
      wanted = dict(sorted(local.items(), reverse=True)[:budget])
      if len(wanted) < len(local):
        self.scan_incomplete = True
      for route_id, route in wanted.items():
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
        self.records[key] = {'source': 'local', 'routeId': route_id, 'status': 'queued', 'signature': signature,
                             'reason': 'Waiting for parked historical review', 'updatedAt': self.clock(), 'summary': None}
      self.last_scan = time.monotonic()
      self._save()

  def snapshot(self):
    with self.lock:
      return {'version': VERSION, 'parked': self._allowed(), 'running': self.running,
              'scanIncomplete': self.scan_incomplete,
              'localRoutes': [{'routeId': name, 'segments': len(route['segments'])} for name, route in sorted(self.local.items(), reverse=True)],
              'reviews': [dict(row) for row in sorted(self.records.values(), key=lambda r: r['updatedAt'], reverse=True)],
              'scope': 'historical_tracking_only', 'automaticApplicationAllowed': False}

  def request(self, source, route_id, *, authorized):
    if not authorized():
      raise PermissionError('Galaxy session expired')
    if source not in ('local', 'connect') or type(route_id) is not str:
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
      if sum(r['status'] == 'queued' and r.get('requested') for r in self.records.values()) >= MAX_QUEUE:
        raise ValueError('The requested-review queue is full; wait for a review to finish')
      old = self.records.get(key)
      if old is None and len(self.records) >= MAX_RECORDS:
        removable = [k for k, r in self.records.items() if r['status'] in ('ready', 'failed') and k != self.running]
        if not removable:
          raise ValueError('The historical review library is full')
        del self.records[min(removable, key=lambda k: self.records[k]['updatedAt'])]
      row = dict(old or {}, source=source, routeId=route_id, status='queued', requested=True,
                 reason='Queued; Connect downloads run only while parked' if source == 'connect' else 'Queued for historical review',
                 updatedAt=self.clock())
      row.setdefault('summary', None)
      self.records[key] = row
      self._save()
      self.wake.set()
    self.start()
    return self.snapshot()

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
      key, row = min(candidates, key=lambda pair: (not pair[1].get('requested', False), -pair[1]['updatedAt'], pair[0]))
      row = dict(row)
      self.running = key
      self.records[key]['status'] = 'analyzing'
      self._save()
    deadline = time.monotonic() + 600
    def permitted():
      return self._allowed() and time.monotonic() < deadline
    try:
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
