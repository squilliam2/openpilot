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
        CREATE TABLE IF NOT EXISTS tracking_windows(number INTEGER NOT NULL, config TEXT NOT NULL, cohort TEXT NOT NULL,
          identity TEXT NOT NULL, stamp INTEGER NOT NULL, previous TEXT NOT NULL, body TEXT NOT NULL,
          PRIMARY KEY(number,identity));
        CREATE INDEX IF NOT EXISTS window_group ON tracking_windows(config,cohort,number,stamp);
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
    prepared, original_windows = [], []
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
      if 'trackingWindows' in group:
        from openpilot.starpilot.flm.canonical_surface_adapter import NAMES, coordinate_bounds
        from openpilot.starpilot.flm.route_window_transport import unpack_window
        from openpilot.starpilot.flm.offroad_evidence import cohort as window_cohort
        previous = group.get('previousCoordinates')
        if (type(group['trackingWindows']) is not list or len(group['trackingWindows']) != windows or
            len(original_windows) + windows > 128 or type(previous) is not dict or set(previous) != set(NAMES) or
            any(type(v) not in (int, float) or not math.isfinite(v) or not coordinate_bounds(n)[0] <= v <= coordinate_bounds(n)[1]
                for n, v in previous.items())):
          raise ValueError('Exact bounded original windows and prior coordinates required')
        for value in group['trackingWindows']:
          window = unpack_window(value)
          if window.points[0].route != self.identity or window.points[0].segment != number or window_cohort(window) != cohort:
            raise ValueError('Original window route, section or cohort changed')
          original_windows.append((number, config, cohort, window.identity, window.points[0].mono_ns,
                                  json.dumps(previous, sort_keys=True), json.dumps(value, separators=(',', ':'), allow_nan=False)))
    with self._connect() as db:
      old = db.execute('SELECT sha FROM sections WHERE number=?', (number,)).fetchone()
      if old:
        if old[0] != sha:
          raise ValueError('Original section bytes changed; restart the route comparison')
        return False
      duplicate = bool(db.execute('SELECT 1 FROM sections WHERE sha=? LIMIT 1', (sha,)).fetchone())
      db.execute('INSERT INTO sections VALUES (?,?,?,?)', (number, sha, int(duplicate), vehicle))
      if not duplicate:
        db.executemany('INSERT INTO tracking_windows VALUES(?,?,?,?,?,?,?)', original_windows)
        for config, cohort, windows, votes in prepared:
          db.execute('INSERT INTO groups VALUES (?,?,?,?)', (number, config, cohort, windows))
          db.executemany('INSERT INTO votes VALUES (?,?,?,?,?,?)', ((number, config, cohort, name, direction, delta)
                                                                  for name, direction, delta in votes))
    return not duplicate

  def summary(self, *, convergence='standard', mode='live', permitted=lambda: True):
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
        return self._pooled_summary(db, report, allowed, convergence, permitted)
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

  @staticmethod
  def _pooled_windows(db, config, cohort, partition):
    from openpilot.starpilot.flm.route_window_transport import unpack_window
    query = 'FROM tracking_windows WHERE config=? AND cohort=? AND number%2=?'
    args = (config, cohort, partition)
    count, sections = db.execute('SELECT count(*),count(DISTINCT number) ' + query, args).fetchone()
    # The upstream numerical estimator accepts at most24 windows. Selection
    # spans the ENTIRE source order before looking at any fit/error outcome.
    indices = list(range(count)) if count <= 24 else [i * (count-1)//23 for i in range(24)]
    selected, previous = [], None
    for index in indices:
      body, prior = db.execute('SELECT body,previous ' + query + ' ORDER BY number,stamp,identity LIMIT 1 OFFSET ?', (*args, index)).fetchone()
      if previous is not None and prior != previous:
        raise ValueError('Original pooled baseline changed within one configuration')
      previous = prior
      selected.append(unpack_window(json.loads(body)))
    return tuple(selected), json.loads(previous) if previous is not None else None, count, sections

  @staticmethod
  def _directions(estimated, windows, previous, cohort, allowed):
    from openpilot.starpilot.flm.offroad_tracking import suggest
    from openpilot.starpilot.flm.offroad_coordinate_policy import tracking_coordinate_candidates
    _, side, phase = cohort.split(':')
    proposals = [suggest(estimated, previous, side=side, phase=phase),
                 *tracking_coordinate_candidates(estimated, windows, previous, side=side, phase=phase)]
    return {p['parameter'].removeprefix('stable:'): p for p in proposals if p is not None and
            p['parameter'].removeprefix('stable:') in allowed and 0 < abs(p['delta']) <= .25}

  def _pooled_summary(self, db, report, allowed, convergence, permitted):
    from openpilot.starpilot.flm.offroad_tracking import estimate
    def fit(windows, cohort):
      try:
        return estimate(windows, cohort=cohort, permitted=permitted)
      except ValueError:
        if not permitted():
          raise InterruptedError('Pooled route comparison paused') from None
        raise
    reasons, diagnostics, chosen, chosen_rank = {}, [], None, None
    count = db.execute('SELECT count(*) FROM tracking_windows').fetchone()[0]
    report['pooledWindowCount'] = count
    if not count:
      return report  # Older immutable reports retain the existing vote path.
    # All ranking uses EVEN source sections only. A single winning direction
    # receives ONE independent odd-section comparison, with no fallback search.
    for config, cohort in db.execute('SELECT DISTINCT config,cohort FROM tracking_windows WHERE number%2=0 ORDER BY config,cohort'):
      if not permitted():
        raise InterruptedError('Pooled route comparison paused')
      windows, previous, total, sections = self._pooled_windows(db, config, cohort, 0)
      reason = 'not_enough_independent_training_sections'
      if sections >= 3 and len(windows) >= 3 and len({w.points[0].segment for w in windows}) >= 3:
        fitted = fit(windows, cohort)
        reason = 'reference_response_not_identified' if fitted is None else 'no_identified_adjustment_in_training_windows'
        choices = {} if fitted is None else self._directions(fitted, windows, previous, cohort, allowed)
        if fitted is not None and len(diagnostics) < 32:
          diagnostics.append({'configurationHash': config, 'cohort': cohort, 'sections': sections, 'windows': total,
                              'gainInterval': fitted.gain_interval, 'lagIntervalMs': [v/1e6 for v in fitted.lag_interval_ns],
                              'residualRms': fitted.residual_rms, 'referenceRms': fitted.reference_rms,
                              'errorReversalHz': fitted.error_reversal_hz, 'identifiedParameters': sorted(choices)})
        for name, proposal in sorted(choices.items()):
          rank = (-sections, -total, config, cohort, name)
          if chosen_rank is None or rank < chosen_rank:
            chosen, chosen_rank = (config, cohort, name, proposal, total, sections,
                                   len(windows), len({w.points[0].segment for w in windows})), rank
        if choices:
          reason = 'training_direction_identified'
      reasons[reason] = reasons.get(reason, 0) + 1
    report['pooledTrainingReasons'] = reasons
    report['pooledTrainingDiagnostics'] = diagnostics
    if chosen is None:
      report['reason'] = 'pooled_training_windows_do_not_identify_a_supported_adjustment'
      return report
    config, cohort, name, proposal, total, sections, fit_windows, fit_sections = chosen
    windows, previous, held_total, held_sections = self._pooled_windows(db, config, cohort, 1)
    comparison = {'parameter': name, 'configurationHash': config, 'cohort': cohort, 'pooledComparison': True,
                  'training': {'sections': sections, 'windows': total, 'fitWindows': fit_windows, 'fitSections': fit_sections},
                  'heldout': {'sections': held_sections, 'windows': held_total, 'fitWindows': len(windows),
                              'fitSections': len({w.points[0].segment for w in windows})}}
    report['comparison'] = comparison
    report['reason'] = 'pooled_heldout_sections_do_not_support_training_selected_direction'
    if held_sections < 3 or len(windows) < 3 or len({w.points[0].segment for w in windows}) < 3:
      return report
    if not permitted():
      raise InterruptedError('Pooled route comparison paused')
    fitted = fit(windows, cohort)
    replication = {} if fitted is None else self._directions(fitted, windows, previous, cohort, allowed)
    other = replication.get(name)
    if other is None or proposal['delta'] * other['delta'] <= 0 or abs(proposal['delta']) > abs(other['delta']) + 1e-12:
      return report
    identities = db.execute('''SELECT DISTINCT s.vehicle FROM sections s JOIN tracking_windows w ON s.number=w.number
                               WHERE w.config=? AND w.cohort=? LIMIT 2''', (config, cohort)).fetchall()
    report['selected'] = {**comparison, 'delta': math.copysign(min(.005, abs(proposal['delta'])), proposal['delta']),
                          'sourceVehicle': json.loads(identities[0][0]) if len(identities) == 1 else None,
                          'convergence': convergence, 'fasterEvidenceSupported': False,
                          'nativeQualified': False, 'requiresFreshNativeQualification': True}
    report['reason'] = 'route_consensus_waiting_for_current_native_trial'
    return report
