"""Checkpoint every original section; route length never selects a sample."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit

from openpilot.starpilot.galaxy.connect_route_review import DEVICE, ROUTE, ReviewUnavailable, RetryableReview
from openpilot.starpilot.galaxy.connect_route_stream import InventoryRows
from openpilot.starpilot.galaxy.drive_history import SEGMENT_NAME

VERSION = 2
FILE_BYTES = 32 * 1024 * 1024
REPORT_BYTES = 1024 * 1024
BATCH_SEGMENTS = 4
PREVIEW = 32


class _LocalRows:
  """Local source revisions are replayed from disk, not a route-length list."""

  def __init__(self, store):
    self.spool = InventoryRows(store)
    self.spool.db.execute('CREATE TABLE local_rows(number INTEGER PRIMARY KEY,body TEXT NOT NULL)')

  def append(self, row):
    try:
      self.spool.db.execute('INSERT INTO local_rows VALUES(?,?)', (row['number'], json.dumps(row, separators=(',', ':'))))
    except sqlite3.IntegrityError:
      raise ReviewUnavailable('Original local segment identity is ambiguous') from None
    self.spool.count += 1
    if self.spool.count % 256 == 0:
      self.spool.db.commit()

  def __len__(self):
    return len(self.spool)

  def __iter__(self):
    if self.spool.closed:
      raise ReviewUnavailable('Local inventory spool is closed')
    for (body,) in self.spool.db.execute('SELECT body FROM local_rows ORDER BY number'):
      yield json.loads(body)

  def close(self):
    self.spool.close()


def original_rows(inventory, identity):
  device, route = identity.split('|')
  if not DEVICE.fullmatch(device) or not ROUTE.fullmatch(route) or type(inventory) is not dict:
    raise ReviewUnavailable('Original Connect route inventory required')
  found = {}
  for urls in inventory.values():
    if type(urls) is not list:
      continue
    for value in urls:
      if type(value) is not str or len(value) > 8192:
        raise ReviewUnavailable('Invalid Connect file location')
      url = urlsplit(value)
      parts = unquote(url.path).split('/')
      if len(parts) < 3 or parts[-1] not in ('rlog.zst', 'rlog.bz2'):
        continue
      if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443) or
          re.fullmatch(r'commadata[a-z0-9-]*\.blob\.core\.windows\.net', url.hostname or '') is None or
          parts[-4:-2] != [device, route] or not parts[-2].isdigit()):
        raise ReviewUnavailable('Connect returned an unexpected original-log location')
      number = int(parts[-2])
      if number > 2**32 - 1 or number in found:
        raise ReviewUnavailable('Duplicate or invalid original segment identity')
      found[number] = {'number': number, 'url': value, 'filename': parts[-1]}
  if not found:
    raise ReviewUnavailable('No original rlogs are available for this Connect route')
  return [found[number] for number in sorted(found)]


def download_original(row, path, *, permitted):
  import requests
  if row.get('local'):
    source = Path(row['local'])
    if any(source.parent.glob('*.lock')):
      raise RetryableReview('Original recording segment is still open; completed progress is saved for retry')
    before = source.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= FILE_BYTES:
      raise ReviewUnavailable('Original dense segment is unavailable')
    with source.open('rb') as incoming, path.open('xb') as outgoing:
      while block := incoming.read(256 * 1024):
        if not permitted():
          raise InterruptedError('Full-route review paused')
        outgoing.write(block)
    after = source.lstat()
    if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) !=
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) or any(source.parent.glob('*.lock'))):
      raise InterruptedError('Original local segment changed')
    return
  try:
    with requests.get(row['url'], stream=True, timeout=(5, 8), allow_redirects=False) as response:
      if response.status_code >= 500 or response.status_code in (408, 429):
        raise RetryableReview('Connect storage is temporarily unavailable; completed progress is saved')
      if response.status_code != 200:
        raise ReviewUnavailable('An original Connect segment could not be downloaded')
      length = response.headers.get('Content-Length')
      if length is not None and (not length.isdigit() or not 0 < int(length) <= FILE_BYTES):
        raise ReviewUnavailable('Original segment exceeds the per-file download budget')
      size = 0
      with path.open('xb') as output:
        for block in response.iter_content(256 * 1024):
          if not permitted():
            raise InterruptedError('Connect import paused')
          size += len(block)
          if size > FILE_BYTES:
            raise ReviewUnavailable('Original segment exceeds the per-file download budget')
          output.write(block)
      if not size:
        raise ReviewUnavailable('Connect returned an empty original segment')
  except requests.RequestException:
    raise RetryableReview('Connect download interrupted; completed progress is saved for retry') from None


def analyze_original(path, codec, *, number, route_id, expected_device, permitted):
  request = json.dumps({'path': str(path), 'codec': codec, 'number': number, 'route_id': route_id,
                        'expected_device': expected_device}).encode()
  with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
    child = subprocess.Popen([sys.executable, '-m', 'openpilot.starpilot.flm.full_route_review'], stdin=subprocess.PIPE,
                             stdout=output, stderr=errors,
                             env=dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1'))
    try:
      child.communicate(request, timeout=.01)
    except subprocess.TimeoutExpired:
      pass
    try:
      deadline = time.monotonic() + 150
      while child.poll() is None:
        if not permitted():
          raise InterruptedError('Full-route import paused when the vehicle started')
        if time.monotonic() >= deadline:
          raise ReviewUnavailable('Original segment reached its processing budget')
        time.sleep(.1)
      if not permitted():
        raise InterruptedError('Full-route import paused')
      if child.returncode:
        raise ReviewUnavailable('Original compressed segment is incomplete or cannot supply usable evidence')
      output.seek(0)
      raw = output.read(REPORT_BYTES + 1)
      if len(raw) > REPORT_BYTES:
        raise ReviewUnavailable('Original segment report exceeds its output budget')
      return json.loads(raw)
    finally:
      if child.poll() is None:
        child.terminate()
        try:
          child.wait(timeout=2)
        except subprocess.TimeoutExpired:
          child.kill()
          child.wait(timeout=2)


class StreamRouteReview:
  def __init__(self, store, *, permitted, log_root=None, device_id=lambda: '', inventory_fetch=None,
               download=None, analyze=None, batch_segments=BATCH_SEGMENTS):
    self.store, self.permitted = Path(store), permitted
    self.log_root, self.device_id = None if log_root is None else Path(log_root), device_id
    self.inventory_fetch = inventory_fetch
    self.download = download_original if download is None else download
    self.analyze = analyze_original if analyze is None else analyze
    if type(batch_segments) is not int or batch_segments < 1:
      raise ValueError('Positive processing batch required')
    self.batch_segments = batch_segments
    self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
    if self.store.is_symlink() or not self.store.is_dir():
      raise ReviewUnavailable('Private import storage is unavailable')

  def _path(self, identity):
    return self.store / (hashlib.sha256(identity.encode()).hexdigest() + '.sqlite3')

  def _db(self, identity):
    path = self._path(identity)
    if path.is_symlink():
      raise ReviewUnavailable('Private import checkpoint is unavailable')
    db = sqlite3.connect(path, timeout=5)
    path.chmod(0o600)
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS segments (number INTEGER PRIMARY KEY,sha TEXT,body TEXT NOT NULL,reason TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY,value INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS vehicles (name TEXT PRIMARY KEY)')
    db.execute('''CREATE TABLE IF NOT EXISTS metrics (config TEXT,domain TEXT,speed TEXT,windows INTEGER,samples INTEGER,
               seconds REAL,squares REAL,absolute REAL,reversals REAL,worst REAL,PRIMARY KEY(config,domain,speed))''')
    return db

  def reset(self, identity):
    with closing(self._db(identity)) as db, db:
      db.execute('DELETE FROM segments')
      db.execute('DELETE FROM meta')
      db.execute('DELETE FROM counters')
      db.execute('DELETE FROM vehicles')
      db.execute('DELETE FROM metrics')

  def local_routes(self):
    routes = {}
    if self.log_root is None or not self.log_root.is_dir():
      return routes
    for folder in self.log_root.iterdir():
      if not self.permitted():
        raise InterruptedError('Local inventory paused')
      match = SEGMENT_NAME.fullmatch(folder.name)
      if match is not None and folder.is_dir() and not folder.is_symlink():
        identity = match.group('route').replace('_', '|')
        number = int(match.group('number'))
        files = {'rlog': any((folder / name).is_file() for name in ('rlog.zst', 'rlog.bz2')), 'qlog': False}
        routes.setdefault(identity, {'routeId': identity, 'segments': []})['segments'].append(
          {'number': number, 'segmentName': folder.name, 'files': files})
    for route in routes.values():
      route['segments'].sort(key=lambda row: row['number'])
    return routes

  def _rows(self, identity, route):
    if route is None:
      if self.inventory_fetch is not None:
        return self.inventory_fetch(identity)
      from openpilot.starpilot.galaxy.connect_route_stream import fetch_inventory
      return fetch_inventory(identity, self.store, permitted=self.permitted)
    if self.log_root is None or route.get('routeId') != identity:
      raise ReviewUnavailable('Original local route required')
    rows = _LocalRows(self.store)
    try:
      for selected in route['segments']:
        if not self.permitted():
          raise InterruptedError('Local inventory paused')
        name = selected['segmentName']
        match = SEGMENT_NAME.fullmatch(name)
        if (match is None or match.group('route').replace('_', '|') != identity or
            type(selected['number']) is not int or selected['number'] != int(match.group('number'))):
          raise ReviewUnavailable('Original local segment identity changed')
        folder = self.log_root / name
        if folder.is_symlink():
          raise ReviewUnavailable('Original local segment location changed')
        names = [name for name in ('rlog.zst', 'rlog.bz2') if (folder / name).is_file()]
        revisions = []
        for path in (folder, *(folder / name for name in (*names, 'rlog.lock', 'qlog.lock'))):
          try:
            info = path.lstat()
            revisions.append((info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
          except FileNotFoundError:
            revisions.append(None)
        rows.append({'number': selected['number'], 'filename': names[0] if len(names) == 1 else 'rlog.zst', 'revision': revisions,
                     'local': str(folder / names[0]) if len(names) == 1 else str(folder / 'rlog.zst')})
      rows.spool.db.commit()
      return rows
    except BaseException:
      rows.close()
      raise

  def run(self, identity, *, purpose='tune', on_progress=None, route=None):
    if not self.permitted():
      raise InterruptedError('Full-route import waits until parked')
    rows = self._rows(identity, route)
    try:
      return self._run_rows(identity, rows, purpose=purpose, on_progress=on_progress)
    finally:
      close = getattr(rows, 'close', None)
      if callable(close):
        close()

  def _run_rows(self, identity, rows, *, purpose, on_progress):
    inventory_hash = hashlib.sha256()
    previous = -1
    missing = 0
    for row in rows:
      number = row['number']
      if type(number) is not int or number <= previous:
        raise ReviewUnavailable('Original route inventory is ambiguous')
      missing += number - previous - 1
      previous = number
      inventory_hash.update(json.dumps((number, row['filename'], row.get('revision')), separators=(',', ':')).encode())
    if previous < 0:
      raise ReviewUnavailable('Original route inventory is empty')
    inventory = inventory_hash.hexdigest()
    with closing(self._db(identity)) as db, db:
      old = db.execute("SELECT value FROM meta WHERE key='inventory'").fetchone()
      if old is not None and old[0] != inventory:
        raise ReviewUnavailable('Original route inventory changed; request a new full review')
      db.execute("INSERT OR REPLACE INTO meta VALUES('inventory',?)", (inventory,))
      db.execute("INSERT OR REPLACE INTO meta VALUES('available',?)", (str(len(rows)),))
      db.execute("INSERT OR REPLACE INTO meta VALUES('gaps',?)", (str(missing),))
      db.execute("INSERT OR REPLACE INTO meta VALUES('purpose',?)", (purpose,))
    done = 0
    for row in rows:
      if not self.permitted():
        break
      with closing(self._db(identity)) as db:
        if db.execute('SELECT 1 FROM segments WHERE number=?', (row['number'],)).fetchone():
          continue
      if done >= self.batch_segments:
        break
      if shutil.disk_usage(self.store).free < FILE_BYTES + 64 * 1024 * 1024:
        raise ReviewUnavailable('Free at least 96 MiB to continue this incremental import')
      reason, digest, result = None, None, {}
      try:
        with tempfile.TemporaryDirectory(prefix='section-', dir=self.store) as folder:
          path = Path(folder) / row['filename']
          self.download(row, path, permitted=self.permitted)
          digest = hashlib.sha256(path.read_bytes()).hexdigest()
          with closing(self._db(identity)) as db:
            duplicate = db.execute('SELECT 1 FROM segments WHERE sha=? AND reason IS NULL', (digest,)).fetchone()
          if duplicate:
            raise ReviewUnavailable('Duplicate original segment bytes; no extra evidence counted')
          result = self.analyze(path, 'zst' if path.name.endswith('.zst') else 'bz2', number=row['number'],
                                route_id=identity, expected_device=identity.split('|')[0] if '|' in identity else self.device_id() or None,
                                permitted=self.permitted)
          if result.get('sourceSha256') != digest or result.get('segment') != row['number'] or result.get('routeId') != identity:
            raise ReviewUnavailable('Original segment report identity changed')
      except InterruptedError:
        break
      except RetryableReview as error:
        with closing(self._db(identity)) as db, db:
          db.execute("INSERT OR REPLACE INTO meta VALUES('retry',?)", (str(error),))
        break
      except ReviewUnavailable as error:
        reason = str(error)
      except (OSError, ValueError, RuntimeError):
        reason = 'Original segment could not be decoded or qualified'
      if not self.permitted():
        break
      with closing(self._db(identity)) as db, db:
        db.execute('INSERT INTO segments VALUES(?,?,?,?)',
                   (row['number'], digest, json.dumps(result, allow_nan=False, separators=(',', ':')), reason))
        prior = db.execute("SELECT value FROM meta WHERE key='digest'").fetchone()
        chain = identity if prior is None else prior[0]
        chain = hashlib.sha256((chain + json.dumps((row['number'], digest, reason))).encode()).hexdigest()
        db.execute("INSERT OR REPLACE INTO meta VALUES('digest',?)", (chain,))
        db.execute("DELETE FROM meta WHERE key='retry'")
        self._accumulate(db, result, reason)
      done += 1
      if on_progress is not None:
        on_progress(self.snapshot(identity))
    return self.snapshot(identity)

  @staticmethod
  def _accumulate(db, result, reason):
    counters = {'attempted': 1, 'omitted' if reason is not None else 'reviewed': 1}
    if reason is None:
      counters.update({key: value for key, value in result.get('counts', {}).items() if type(value) is int and value >= 0})
      if result.get('knownVinRecorded') is True:
        counters['knownVin'] = 1
      db.executemany('INSERT OR IGNORE INTO vehicles VALUES(?)', ((name,) for name in result.get('vehicles', ())))
      for metric in result.get('metrics', ()):
        db.execute('''INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(config,domain,speed) DO UPDATE SET
          windows=windows+excluded.windows,samples=samples+excluded.samples,seconds=seconds+excluded.seconds,
          squares=squares+excluded.squares,absolute=absolute+excluded.absolute,reversals=reversals+excluded.reversals,
          worst=max(worst,excluded.worst)''', (metric['configurationHash'], metric['domain'], metric['speedRange'],
          metric['windows'], metric['samples'], metric['observedSeconds'], metric['rmse']**2 * metric['samples'],
          metric['meanAbsoluteError'] * metric['samples'], metric['errorReversalHz'] * metric['observedSeconds'], metric['worstAbsoluteError']))
    for key, value in counters.items():
      db.execute('INSERT INTO counters VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value', (key, value))

  def iter_segments(self, identity):
    with closing(self._db(identity)) as db:
      for (body,) in db.execute('SELECT body FROM segments WHERE reason IS NULL ORDER BY number'):
        yield json.loads(body)

  def snapshot(self, identity):
    with closing(self._db(identity)) as db:
      meta = dict(db.execute('SELECT key,value FROM meta'))
      counts = dict(db.execute('SELECT key,value FROM counters'))
      attempted, successful = counts.pop('attempted', 0), counts.pop('reviewed', 0)
      counts.pop('omitted', None)
      known_vin = bool(counts.pop('knownVin', 0))
      vehicles = [row[0] for row in db.execute('SELECT name FROM vehicles ORDER BY name LIMIT 32')]
      reviewed = [row[0] for row in db.execute('SELECT number FROM segments WHERE reason IS NULL ORDER BY number LIMIT ?', (PREVIEW,))]
      omitted = [list(row) for row in db.execute('SELECT number,reason FROM segments WHERE reason IS NOT NULL ORDER BY number LIMIT ?', (PREVIEW,))]
      group_count = db.execute('SELECT count(*) FROM metrics').fetchone()[0]
      metrics = []
      for config, domain, speed, windows, samples, seconds, squares, absolute, reversals, worst in db.execute(
          'SELECT * FROM metrics ORDER BY samples DESC,config,domain,speed LIMIT 24'):
        metrics.append({'configurationHash': config, 'domain': domain, 'speedRange': speed, 'windows': windows, 'samples': samples,
                        'observedSeconds': seconds, 'rmse': (squares / max(1, samples))**.5,
                        'meanAbsoluteError': absolute / max(1, samples), 'worstAbsoluteError': worst,
                        'errorReversalHz': reversals / max(1e-12, seconds)})
    available = int(meta.get('available', '0'))
    complete = attempted == available and available > 0
    return {'version': 2, 'routeId': identity, 'vehicles': sorted(vehicles), 'knownVinRecorded': known_vin,
            'availableSegments': available, 'attemptedSegments': attempted, 'reviewedCount': successful,
            'omittedCount': attempted - successful, 'reviewedSegments': reviewed, 'omittedSegments': omitted,
            'segmentPreviewTruncated': successful > len(reviewed) or attempted - successful > len(omitted),
            'allSegmentsAttempted': complete, 'wholeRouteReviewed': complete and successful == available and meta.get('gaps', '0') == '0',
            'inventoryGapCount': int(meta.get('gaps', '0')), 'state': 'ready' if complete else 'queued',
            'sourceSha256': meta.get('digest', hashlib.sha256(identity.encode()).hexdigest()),
            'retryReason': meta.get('retry'), 'counts': counts, 'metrics': metrics, 'metricPreviewTruncated': group_count > 24,
            'analysisOnly': meta.get('purpose') != 'tune', 'automaticApplicationAllowed': False, 'tuneChanged': False,
            'limitation': 'Every available segment is attempted. Original gaps and missing evidence remain explicit; fresh qualification precedes application.'}
