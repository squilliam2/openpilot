"""All-segment directional comparison; its output is intent, never permission."""

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from openpilot.starpilot.flm.offroad_surface import ROUTE_NAMES

VERSION = 1


def _digest(value):
  return type(value) is str and len(value) == 64 and not value.strip('0123456789abcdef')


def _lower(success, total):
  """One-sided 95% Wilson lower bound; votes are original source sections."""
  if total <= 0:
    return 0.
  z = 1.6448536269514722
  probability = success / total
  square = z * z
  return (probability + square / (2 * total) - z * math.sqrt(probability * (1 - probability) / total + square / (4 * total * total))) / (1 + square / total)


class RouteConsensus:
  """Disk-backed sufficient statistics, with no route-wide segment-count cap.

  Even original section numbers train; odd numbers validate. Selection reads
  training votes only. The single selected coordinate then gets one held-out
  comparison. No second candidate is tried against that same held-out data.
  """

  def __init__(self, path, identity):
    if type(identity) is not str or not 1 <= len(identity) <= 180:
      raise ValueError('Original route identity required')
    self.path, self.identity = Path(path), identity
    self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if self.path.is_symlink():
      raise ValueError('Consensus store must not be a symlink')
    with self._connect() as db:
      db.executescript('''
        CREATE TABLE IF NOT EXISTS identity(version INTEGER NOT NULL, route TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sections(number INTEGER PRIMARY KEY, sha TEXT NOT NULL,
          duplicate INTEGER NOT NULL, vehicle TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS groups(number INTEGER NOT NULL, config TEXT NOT NULL, cohort TEXT NOT NULL,
          windows INTEGER NOT NULL, PRIMARY KEY(number,config,cohort));
        CREATE TABLE IF NOT EXISTS votes(number INTEGER NOT NULL, config TEXT NOT NULL, cohort TEXT NOT NULL,
          parameter TEXT NOT NULL, direction INTEGER NOT NULL, delta REAL NOT NULL,
          PRIMARY KEY(number,config,cohort,parameter));
        CREATE INDEX IF NOT EXISTS vote_group ON votes(config,cohort,parameter);
        CREATE INDEX IF NOT EXISTS source_hash ON sections(sha);
      ''')
      row = db.execute('SELECT version,route FROM identity').fetchall()
      if not row:
        db.execute('INSERT INTO identity VALUES (?,?)', (VERSION, identity))
      elif row != [(VERSION, identity)]:
        raise ValueError('Consensus database belongs to another route/version')
    self.path.chmod(0o600)

  @contextmanager
  def _connect(self):
    if self.path.is_symlink():
      raise ValueError('Consensus store must not be a symlink')
    db = sqlite3.connect(self.path, timeout=10)
    try:
      db.execute('PRAGMA temp_store=FILE')
      db.execute('PRAGMA cache_size=-1024')
      with db:
        yield db
    finally:
      db.close()

  def add(self, result):
    number, sha = result.get('segment'), result.get('sourceSha256')
    if type(number) is not int or number < 0 or not _digest(sha):
      raise ValueError('Original section number and bytes hash required')
    groups = result.get('proposalGroups', [])
    if type(groups) is not list or len(groups) > 256:
      raise ValueError('Bounded per-section original tracking contexts required')
    vehicle = json.dumps(result.get('vehicleIdentity'), sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(vehicle) > 4096:
      raise ValueError('Bounded original vehicle identity required')
    prepared = []
    seen = set()
    for group in groups:
      config, cohort, windows = group.get('configurationHash'), group.get('cohort'), group.get('windows')
      if (not _digest(config) or type(cohort) is not str or not 1 <= len(cohort) <= 160 or
          type(windows) is not int or windows < 1 or (config, cohort) in seen):
        raise ValueError('Distinct original eligible tracking groups required')
      seen.add((config, cohort))
      proposals = group.get('proposals', [])
      if type(proposals) is not list or len(proposals) > 4 * len(ROUTE_NAMES):
        raise ValueError('Bounded original coordinate suggestions required')
      by_name = {}
      for proposal in proposals:
        name, delta = proposal.get('parameter'), proposal.get('delta')
        if name not in ROUTE_NAMES or type(delta) not in (int, float) or not math.isfinite(delta) or abs(delta) > .25:
          raise ValueError('A finite original coordinate suggestion is required')
        by_name.setdefault(name, []).append(float(delta))
      votes = []
      for name, deltas in sorted(by_name.items()):
        directions = {1 if delta > 0 else -1 if delta < 0 else 0 for delta in deltas}
        # A conflicting section is one non-supporting vote, never two votes.
        direction = next(iter(directions)) if len(directions) == 1 else 0
        delta = min(map(abs, deltas)) * direction
        votes.append((name, direction, delta))
      prepared.append((config, cohort, windows, votes))
    with self._connect() as db:
      old = db.execute('SELECT sha FROM sections WHERE number=?', (number,)).fetchone()
      if old:
        if old[0] != sha:
          raise ValueError('Original section bytes changed; restart the route comparison')
        return False
      duplicate = bool(db.execute('SELECT 1 FROM sections WHERE sha=? LIMIT 1', (sha,)).fetchone())
      db.execute('INSERT INTO sections VALUES (?,?,?,?)', (number, sha, int(duplicate), vehicle))
      if not duplicate:
        for config, cohort, windows, votes in prepared:
          db.execute('INSERT INTO groups VALUES (?,?,?,?)', (number, config, cohort, windows))
          db.executemany('INSERT INTO votes VALUES (?,?,?,?,?,?)', ((number, config, cohort, name, direction, delta)
                                                                  for name, direction, delta in votes))
    return not duplicate

  def summary(self, *, convergence='standard', mode='live'):
    if convergence not in ('standard', 'faster') or mode not in ('live', 'parked'):
      raise ValueError('Known convergence policy and learning mode required')
    from openpilot.starpilot.flm.canonical_surface_adapter import NAMES
    allowed = set(NAMES if mode == 'live' else ROUTE_NAMES)
    with self._connect() as db:
      digest = hashlib.sha256((self.identity + '\n').encode())
      for row in db.execute('SELECT number,sha,duplicate FROM sections ORDER BY number'):
        digest.update(json.dumps(row, separators=(',', ':')).encode() + b'\n')
      count, duplicates = db.execute('SELECT count(*),coalesce(sum(duplicate),0) FROM sections').fetchone()
      # This query sees training only. Held-out scores cannot rank parameters.
      chosen = None
      chosen_rank = None
      for config, cohort, name, direction, support in db.execute('''
          SELECT config,cohort,parameter,direction,count(*) FROM votes
          WHERE number%2=0 AND direction!=0 GROUP BY config,cohort,parameter,direction'''):
        if name not in allowed:
          continue
        total = db.execute('SELECT count(*) FROM groups WHERE config=? AND cohort=? AND number%2=0', (config, cohort)).fetchone()[0]
        if total >= 3 and support / total >= .8 and _lower(support, total) > .5:
          candidate = (support, _lower(support, total), config, cohort, name, direction, total)
          rank = (-support, -candidate[1], config, cohort, name, direction)
          if chosen_rank is None or rank < chosen_rank:
            chosen, chosen_rank = candidate, rank
      report = {'version': VERSION, 'routeId': self.identity, 'sourceSha256': digest.hexdigest(),
                'processedSegments': count, 'duplicateSegments': duplicates, 'eligibleSegments': count - duplicates,
                'partition': 'even_original_segments_training_odd_original_segments_heldout',
                'selectionPolicy': 'training_ranked_one_candidate_one_heldout_comparison',
                'selected': None, 'reason': 'more_independent_consistent_segment_evidence_required',
                'automaticApplicationAllowed': False, 'requiresFreshNativeQualification': True}
      if chosen is None:
        return report
      support, lower, config, cohort, name, direction, total = chosen
      heldout_total = db.execute('SELECT count(*) FROM groups WHERE config=? AND cohort=? AND number%2=1', (config, cohort)).fetchone()[0]
      heldout_support = db.execute('SELECT count(*) FROM votes WHERE config=? AND cohort=? AND parameter=? AND direction=? AND number%2=1',
                                   (config, cohort, name, direction)).fetchone()[0]
      comparison = {'parameter': name, 'configurationHash': config, 'cohort': cohort,
                    'training': {'support': support, 'total': total, 'lowerBound': lower},
                    'heldout': {'support': heldout_support, 'total': heldout_total, 'lowerBound': _lower(heldout_support, heldout_total)}}
      report['comparison'] = comparison
      if heldout_total < 3 or heldout_support / heldout_total < .8 or _lower(heldout_support, heldout_total) <= .5:
        report['reason'] = 'heldout_sections_do_not_support_training_selected_direction'
        return report
      identities = db.execute('''SELECT DISTINCT s.vehicle FROM sections s JOIN groups g ON s.number=g.number
                                 WHERE g.config=? AND g.cohort=? LIMIT 2''', (config, cohort)).fetchall()
      source_vehicle = json.loads(identities[0][0]) if len(identities) == 1 else None
      faster_supported = (min(total, heldout_total) >= 12 and min(lower, _lower(heldout_support, heldout_total)) >= .7
                          and min(support / total, heldout_support / heldout_total) >= .95)
      report['selected'] = {**comparison, 'delta': direction * .005, 'sourceVehicle': source_vehicle,
                            'convergence': convergence, 'fasterEvidenceSupported': faster_supported,
                            'nativeQualified': False, 'requiresFreshNativeQualification': True}
      report['reason'] = 'route_consensus_waiting_for_current_native_trial'
      return report
