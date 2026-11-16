"""Durable cloud intent queue; accepted maps and actuator commands are never written."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat

from opendbc.car.vin import VIN_UNKNOWN, is_valid_vin
from openpilot.starpilot.flm.canonical_surface_adapter import NAMES, coordinate_bounds
from openpilot.starpilot.flm.offroad_cache import AssessmentCache, directory_for_params
from openpilot.starpilot.flm.offroad_store import RouteScope
from openpilot.starpilot.flm.offroad_surface import ROUTE_NAMES, route_bounds
from openpilot.starpilot.saved_source import read_saved

VERSION = 1
MAX_BYTES = 256 * 1024
MAX_PENDING = 32  # Job retention, not a route's segment count.
STATES = frozenset(('waiting_for_vehicle', 'queued', 'native_trial_offered', 'confirmed', 'rolled_back', 'rejected', 'stale_scope'))
TERMINAL = frozenset(('confirmed', 'rolled_back', 'rejected', 'stale_scope'))


def _hash(value):
  return type(value) is str and len(value) == 64 and not value.strip('0123456789abcdef')


def _canonical(value):
  return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def vehicle_identity(cp, device_id):
  firmware = sorted((str(row.ecu), int(row.address), int(row.subAddress), bytes(row.fwVersion).hex()) for row in cp.carFw)
  return {'fingerprint': str(cp.carFingerprint), 'vin': str(cp.carVin),
          'firmwareSha256': hashlib.sha256(_canonical(firmware)).hexdigest() if firmware and any(row[3] for row in firmware) else None,
          'deviceId': device_id}


def same_vehicle(observed, current):
  if type(observed) is not dict or type(current) is not dict or observed.get('fingerprint') != current.get('fingerprint'):
    return False
  vin, now_vin = observed.get('vin'), current.get('vin')
  if type(vin) is str and type(now_vin) is str and vin != VIN_UNKNOWN and now_vin != VIN_UNKNOWN and is_valid_vin(vin) and is_valid_vin(now_vin):
    return vin == now_vin
  device, firmware = observed.get('deviceId'), observed.get('firmwareSha256')
  return (type(device) is str and re.fullmatch('[0-9a-f]{16}', device) is not None and device == current.get('deviceId')
          and _hash(firmware) and firmware == current.get('firmwareSha256'))


def _cache(params):
  root = directory_for_params(params)
  return AssessmentCache(root.with_name(root.name + '-route-imports'))


@contextmanager
def _locked(cache):
  cache._owned()
  fd = os.open(cache.directory / '.imports.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
      raise ValueError('Private route import lock required')
    fcntl.flock(fd, fcntl.LOCK_EX)
    yield
  finally:
    os.close(fd)


def _read(cache):
  try:
    value = json.loads(cache._read('pending.json', MAX_BYTES))
  except FileNotFoundError:
    return {}
  if (type(value) is not dict or value.get('version') != VERSION or type(value.get('intents')) is not dict or
      len(value['intents']) > MAX_PENDING):
    raise ValueError('Valid bounded import queue required')
  rows = value['intents']
  for identity, row in rows.items():
    if (not _hash(identity) or type(row) is not dict or row.get('state') not in STATES or
        not _hash(row.get('source_sha256')) or row.get('name') not in ROUTE_NAMES or
        type(row.get('delta')) not in (int, float) or not math.isfinite(row['delta']) or not 0 < abs(row['delta']) <= .005):
      raise ValueError('Valid source-bound route import required')
  return rows


def _save(cache, rows):
  cache._write('pending.json', _canonical({'version': VERSION, 'intents': rows}), maximum=MAX_BYTES)


@contextmanager
def _archive(cache):
  """Retain terminal source IDs on disk so completed jobs never fill the queue."""
  path = cache.directory / 'completed.sqlite3'
  fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
      raise ValueError('Private completed route import store required')
    db = sqlite3.connect(path, timeout=10)
    try:
      current = path.lstat()
      if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        raise ValueError('Completed import store changed')
      with db:
        db.execute('CREATE TABLE IF NOT EXISTS completed(identity TEXT PRIMARY KEY, updated REAL NOT NULL, body TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS completed_updated ON completed(updated)')
        yield db
    finally:
      db.close()
  finally:
    os.close(fd)


def enqueue(params, *, route_id, source_sha256, proposal, observed_vehicle=None, authorized=lambda: True):
  """Persist numerical intent even before the car has been identified locally."""
  if not authorized():
    raise ValueError('Route import was interrupted')
  if type(route_id) is not str or not 1 <= len(route_id) <= 180 or not _hash(source_sha256):
    raise ValueError('Original route identity and source digest required')
  name, delta = proposal.get('parameter'), proposal.get('delta')
  if (name not in ROUTE_NAMES or type(delta) not in (int, float) or not math.isfinite(delta) or not 0 < abs(delta) <= .005 or
      proposal.get('requiresFreshNativeQualification') is not True or proposal.get('nativeQualified') is not False):
    raise ValueError('Unqualified bounded proposal intent required')
  vehicle = observed_vehicle if observed_vehicle is not None else proposal.get('sourceVehicle')
  row = {'route_id': route_id, 'source_sha256': source_sha256, 'name': name, 'delta': float(delta),
         'vehicle': vehicle, 'proposal': proposal, 'binding': None, 'previous': None, 'mode': None,
         'state': 'waiting_for_vehicle', 'updated_at': datetime.now(UTC).timestamp(),
         'reason': 'current_vehicle_and_native_qualification_required'}
  intent_id = hashlib.sha256(_canonical((route_id, source_sha256, name, 1 if delta > 0 else -1))).hexdigest()
  cache = _cache(params)
  with _locked(cache):
    rows = _read(cache)
    if intent_id not in rows:
      completed = []
      with _archive(cache) as db:
        if db.execute('SELECT 1 FROM completed WHERE identity=?', (intent_id,)).fetchone():
          return intent_id
        if len(rows) >= MAX_PENDING:
          completed = [identity for identity, saved in rows.items() if saved['state'] in TERMINAL]
          for identity in completed:
            saved = rows[identity]
            db.execute('INSERT OR IGNORE INTO completed VALUES(?,?,?)',
                       (identity, saved['updated_at'], _canonical(saved).decode()))
          # Commit archive IDs before removing JSON rows. An interruption may
          # leave a duplicate terminal row, but can never erase its dedup ID.
      for identity in completed:
        del rows[identity]
      if len(rows) >= MAX_PENDING:
        raise ValueError('Route import job queue is full; no saved intent was discarded')
      if not authorized():
        raise ValueError('Route import was interrupted')
      rows[intent_id] = row
      _save(cache, rows)
  return intent_id


@dataclass(frozen=True)
class PendingImport:
  intent_id: str
  route_id: str
  source_sha256: str
  scope: RouteScope
  previous: tuple
  mode: str
  name: str
  delta: float
  convergence: str
  faster_evidence_supported: bool

  def as_parked_candidate(self):
    if self.mode != 'live':
      raise ValueError('Only live canonical imports select a live candidate')
    from openpilot.starpilot.flm.live_parked_finish import ParkedCandidate
    return ParkedCandidate(self.scope, self.previous, self.name, self.delta, self.source_sha256).validate()


def _previous(previous, mode):
  previous = dict(previous)
  names = NAMES if mode == 'live' else ROUTE_NAMES
  bounds = coordinate_bounds if mode == 'live' else route_bounds
  if set(previous) != set(names):
    raise ValueError('Exact current accepted profile required')
  for name, value in previous.items():
    low, high = bounds(name)
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
      raise ValueError('Current accepted coordinate outside its existing bounds')
  return tuple((name, previous[name]) for name in names)


def load_pending(params, cp, scope, previous, mode):
  """Bind once to the current real vehicle/settings/profile; never rebase a trial."""
  from openpilot.starpilot.flm.offroad_runtime import current_scope
  from openpilot.starpilot.flm.learning_mode import selected_mode
  from openpilot.starpilot.flm.pid_stages import UNSUPPORTED
  from openpilot.starpilot.flm.offroad_surface import CURVE_NAMES
  if mode not in ('live', 'parked') or selected_mode(params) != mode or current_scope(cp, params) != scope:
    return None
  scope.validate()
  previous = _previous(previous, mode)
  device_raw, okay = read_saved(params, 'DongleId', 64)
  device = device_raw.decode('ascii') if okay and device_raw is not None else None
  current_vehicle = vehicle_identity(cp, device)
  cache = _cache(params)
  selected = None
  with _locked(cache):
    rows, changed = _read(cache), False
    for identity, row in sorted(rows.items(), key=lambda item: (-item[1].get('updated_at', 0), item[0])):
      if row['state'] in TERMINAL or row['name'] not in (NAMES if mode == 'live' else ROUTE_NAMES):
        continue
      if not same_vehicle(row.get('vehicle'), current_vehicle):
        continue  # Keep a pending job, but never aim another car's intent here.
      if scope.controller_domain == 'torque_pid' and row['name'] in (*UNSUPPORTED, *CURVE_NAMES):
        row.update(state='rejected', reason='current_controller_does_not_support_this_coordinate')
        changed = True
        continue
      binding = row.get('binding')
      if binding is None:
        row.update(binding=scope.document(), previous=[list(pair) for pair in previous], mode=mode,
                   state='queued', reason='waiting_for_fresh_current_native_trial')
        changed = True
      elif binding != scope.document() or row.get('mode') != mode or row.get('previous') != [list(pair) for pair in previous]:
        row.update(state='stale_scope', reason='vehicle_settings_reset_or_accepted_profile_changed')
        changed = True
        continue
      proposal = row['proposal']
      selected = PendingImport(identity, row['route_id'], row['source_sha256'], scope, previous, mode, row['name'], row['delta'],
                               proposal.get('convergence', 'standard'), proposal.get('fasterEvidenceSupported') is True)
      break
    if changed:
      if current_scope(cp, params) != scope:
        return None
      _save(cache, rows)
  return selected


def mark_state(params, intent_id, state, *, runtime_event=False, reason=None):
  if not _hash(intent_id) or state not in STATES:
    raise ValueError('Known route import identity and state required')
  if state in ('native_trial_offered', 'confirmed', 'rolled_back') and runtime_event is not True:
    raise ValueError('Only the native trial lifecycle can report application outcomes')
  cache = _cache(params)
  with _locked(cache):
    rows = _read(cache)
    with _archive(cache) as db:
      row = rows.get(intent_id)
      archived = row is None
      if archived:
        saved = db.execute('SELECT body FROM completed WHERE identity=?', (intent_id,)).fetchone()
        row = json.loads(saved[0]) if saved is not None else None
      if row is None:
        return False
      # Parked trials can be withdrawn after initial acceptance. They remain
      # terminal; the old imported recording can never requeue itself.
      later_native_rollback = row['state'] == 'confirmed' and state == 'rolled_back' and runtime_event is True
      if row['state'] in TERMINAL and row['state'] != state and not later_native_rollback:
        return False
      if state == 'native_trial_offered' and row['state'] not in ('queued', 'native_trial_offered'):
        return False
      if state in ('confirmed', 'rolled_back') and row['state'] not in ('native_trial_offered', state) and not later_native_rollback:
        return False
      row.update(state=state, updated_at=datetime.now(UTC).timestamp())
      if reason is not None:
        if type(reason) is not str or len(reason) > 240:
          raise ValueError('Bounded import lifecycle reason required')
        row['reason'] = reason
      if archived:
        db.execute('UPDATE completed SET updated=?,body=? WHERE identity=?', (row['updated_at'], _canonical(row).decode(), intent_id))
      else:
        _save(cache, rows)
      return True


def snapshot(params):
  cache = _cache(params)
  with _locked(cache):
    rows = _read(cache)
    with _archive(cache) as db:
      for identity, raw in db.execute('SELECT identity,body FROM completed ORDER BY updated DESC LIMIT 100'):
        rows.setdefault(identity, json.loads(raw))
  return [{'intentId': identity, 'routeId': row['route_id'], 'state': row['state'], 'reason': row['reason'],
           'parameter': row['name'], 'delta': row['delta'], 'requiresFreshNativeQualification': True,
           'acceptedMapWrittenByImport': False} for identity, row in sorted(rows.items())]
