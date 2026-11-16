"""Interrupted recording fixtures; no route ending or accepted tune is forged."""

import bz2
import math
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opendbc.car.car_helpers import interfaces
from opendbc.car.toyota.values import CAR
from openpilot.cereal import log, messaging
from openpilot.common.params import Params
from openpilot.starpilot.flm.live_parked_finish import LiveParkedFinisher, load_candidate, trim_unobserved_edges
from openpilot.starpilot.flm.offroad_evidence import RoutePoint, RouteWindow, window_identity
from openpilot.starpilot.flm.offroad_partial import partial_inventory, walk_partial_route
from openpilot.starpilot.flm.offroad_route import PartialRouteReceipt, RouteInputError, RouteInputCancelled
from openpilot.starpilot.flm.offroad_runtime import current_scope
from openpilot.starpilot.schema_cache import put_cache


def saved_vehicle(tmp_path):
  params = Params(str(tmp_path / 'params'))
  cp = interfaces[CAR.TOYOTA_RAV4_TSS2].get_non_essential_params(CAR.TOYOTA_RAV4_TSS2)
  cp.carVin = 'JTMAB3FV0MD000001'
  params.put_bool('IsOffroad', True, block=True)
  params.put_bool('OpenpilotEnabledToggle', True, block=True)
  params.put('RouteCount', 2, block=True)
  put_cache(params, 'CarParamsPersistent', cp, block=True)
  assert params.get('CurrentRoute') is None  # An indoor reboot clears this key.
  return params, cp, current_scope(cp, params)


def closed_section(root, cp, scope, number, *, route_end=False):
  folder = root / f'00000001--aaaaaaaaaa--{number}'
  folder.mkdir(parents=True)
  first = 1_000_000_000 + number * 60_000_000_000
  events = [log.Event.new_message(logMonoTime=first, valid=True, carParams=cp)]
  for index in range(6000):
    stamp = first + index * 10_000_000
    cs = log.Event.new_message(logMonoTime=stamp, valid=True)
    cs.init('carState').vEgo, cs.carState.canValid = 20., True
    cc = log.Event.new_message(logMonoTime=stamp, valid=True)
    cc.init('carControl').latActive = True
    metadata = messaging.new_message('starpilotLateralState', valid=True, logMonoTime=stamp)
    metadata.starpilotLateralState.learningEvidence = {
      'version': 1, 'controlMonoTime': stamp, 'sourceStateMonoTime': stamp, 'sourceReceiptMonoTime': stamp,
      'enabled': True, 'active': True, 'valid': True, 'controllerMode': scope.controller_mode,
      'controllerRevision': scope.controller_revision, 'baselineRevision': scope.baseline_revision, 'baselineValues': [1.],
      'stageSchemaRevision': 'torque-selected-stage-snapshot-v1',
    }
    state = log.Event.new_message(logMonoTime=stamp, valid=True)
    actual = state.init('controlsState').lateralControlState.init('torqueState')
    desired = .4 + .2 * math.sin(2 * math.pi * .7 * index / 100)
    actual.active, actual.desiredLateralAccel, actual.actualLateralAccel = True, desired, .85 * desired
    events.extend((cs, cc, metadata, state))
  clock = log.Event.new_message(logMonoTime=first + 60_000_000_000, valid=True)
  clock.init('clocks').wallTimeNanos = 1_800_000_000_000_000_000 + number * 60_000_000_000
  ending = log.Event.new_message(logMonoTime=clock.logMonoTime, valid=True)
  ending.init('sentinel').type = 'endOfRoute' if route_end else 'endOfSegment'
  events.extend((clock, ending))
  path = folder / 'rlog.bz2'
  path.write_bytes(bz2.compress(b''.join(event.to_bytes() for event in events)))
  return path


@pytest.mark.parametrize('tail', ['locked', 'empty', 'truncated', 'missing_route_end'])
def test_indoor_reboot_finishes_intact_sections_without_changing_recordings_or_accepted_tunes(tmp_path, tail):
  params, cp, scope = saved_vehicle(tmp_path)
  root = tmp_path / 'logs'
  first = closed_section(root, cp, scope, 0)
  lock = None
  if tail != 'missing_route_end':
    folder = root / '00000001--aaaaaaaaaa--1'
    folder.mkdir()
    if tail == 'locked':
      lock = folder / 'rlog.lock'
      lock.touch()
      (folder / 'rlog.bz2').write_bytes(b'not finalized')
    elif tail == 'truncated':
      (folder / 'rlog.bz2').write_bytes(b'BZh9interrupted compressor')
  originals = {path: path.read_bytes() for path in root.glob('*/*')}
  protected = {key: params.get(key) for key in ('AutomaticSteeringProfiles', 'AutomaticSteeringLiveTrials', 'SteeringLearningPreferences')}
  worker = LiveParkedFinisher(params, root, permitted=lambda: True)
  result = worker.run_once()  # Actual isolated process, Params and Cereal readers.
  assert result['finishing_completed'] and result['partial_drive']
  assert result['partial_segments'] == [1, 1 if tail == 'missing_route_end' else 2]
  assert result['recorded_cutoff_ns'] == 61_000_000_000
  assert not result['route_completion_proven'] and not result['actual_application_proven']
  assert not result['configuration_changed'] and result['requires_fresh_live_qualification']
  candidate = load_candidate(params)
  assert candidate is not None and candidate.document()['requires_fresh_live_qualification']
  assert protected == {key: params.get(key) for key in protected}
  assert originals == {path: path.read_bytes() for path in root.glob('*/*')}
  if lock is not None:
    assert lock.exists()
  with patch('openpilot.starpilot.flm.offroad_partial.walk_partial_route', side_effect=AssertionError('Unchanged recording decoded twice')):
    assert worker._run_once() == result
  assert first.exists()


def test_closed_segment_without_route_end_is_partial_and_complete_route_still_uses_full_reader(tmp_path):
  from openpilot.starpilot.flm.tests.test_offroad_fault_isolation import TestPartialDriveReview
  maker = TestPartialDriveReview()
  name, path, _ = maker.segment(tmp_path, 18, 0)
  before = path.read_bytes()
  route = partial_inventory(tmp_path, name, permitted=lambda: True)
  receipt = walk_partial_route(tmp_path, route, permitted=lambda: True, consume=lambda *args: None)
  assert type(receipt) is PartialRouteReceipt and receipt.decoded_segments == (0,)
  assert receipt.omitted_segments == () and receipt.completed_wall_ns == 0
  assert path.read_bytes() == before
  maker.segment(tmp_path, 18, 1, ending='endOfRoute')
  route = partial_inventory(tmp_path, name, permitted=lambda: True)
  with pytest.raises(RouteInputError, match='Complete input'):
    walk_partial_route(tmp_path, route, permitted=lambda: True, consume=lambda *args: None)


def test_one_interrupted_first_segment_has_no_invented_recovery(tmp_path):
  params, _, _ = saved_vehicle(tmp_path)
  root = tmp_path / 'logs'
  folder = root / '00000001--aaaaaaaaaa--0'
  folder.mkdir(parents=True)
  lock = folder / 'rlog.lock'
  lock.touch()
  result = LiveParkedFinisher(params, root, permitted=lambda: True).run_once()
  assert result['status'] == 'unavailable' and 'No independently closed' in result['reason']
  assert not result.get('finishing_completed') and not result.get('partial_drive')
  assert not result['configuration_changed'] and load_candidate(params) is None and lock.exists()


def test_repair_replaces_partial_fit_without_promoting_saved_history(tmp_path):
  params, cp, scope = saved_vehicle(tmp_path)
  root = tmp_path / 'logs'
  closed_section(root, cp, scope, 0)
  folder = root / '00000001--aaaaaaaaaa--1'
  folder.mkdir()
  lock = folder / 'rlog.lock'
  lock.touch()
  worker = LiveParkedFinisher(params, root, permitted=lambda: True)
  before = worker.run_once()
  assert before['partial_drive']
  lock.unlink()  # Fixture repair only; production code never removes locks.
  folder.rmdir()
  closed_section(root, cp, scope, 1, route_end=True)
  after = worker.run_once()
  assert after['finishing_completed'] and not after.get('partial_drive')
  assert after['source_sha256'] != before['source_sha256']
  assert params.get('AutomaticSteeringProfiles') is None and params.get('AutomaticSteeringLiveTrials') is None


def test_unknown_adjacent_sections_remove_boundary_windows_only():
  def window(number, start):
    points = tuple(RoutePoint('declared', number, int((start + i / 100) * 1e9), 'lateral_acceleration', 'a' * 64,
                              .5, .4, 20., .1, .1, 1, 1, True, False, False, True) for i in range(201))
    return RouteWindow(window_identity(points), points)
  first, clean, last = window(0, 1.), window(0, 4.), window(0, 11.)
  following = window(2, 25.)
  inputs = SimpleNamespace(segment_bounds={0: (1_000_000_000, 13_000_000_000), 2: (25_000_000_000, 37_000_000_000)},
                           windows={'x': [first, clean, last, following]}, stable_windows={}, baseline_tracking={}, raw_windows={}, counts={})
  trim_unobserved_edges(inputs)
  assert inputs.windows['x'] == [clean]
  assert inputs.counts['unobserved_boundary_excluded_windows'] == 3


def test_partial_355_segment_drive_uses_independent_bounded_selection(tmp_path):
  from openpilot.starpilot.flm.tests.test_offroad_fault_isolation import TestPartialDriveReview
  maker = TestPartialDriveReview()
  for number in range(355):
    name, _, _ = maker.segment(tmp_path, 18, number)
  route = partial_inventory(tmp_path, name, permitted=lambda: True)
  consumed = []
  receipt = walk_partial_route(tmp_path, route, permitted=lambda: True,
                               consume=lambda number, events: consumed.append(number), selection_limit=14)
  assert consumed == [index * 354 // 13 for index in range(14)]
  assert receipt.decoded_segments == tuple(consumed) and len(receipt.omitted_segments) == 341
  assert receipt.completed_wall_ns == 0


def test_cancellation_after_closed_section_cannot_publish_partial_success(tmp_path):
  from openpilot.starpilot.flm.tests.test_offroad_fault_isolation import TestPartialDriveReview
  maker = TestPartialDriveReview()
  name, _, _ = maker.segment(tmp_path, 18, 0)
  maker.segment(tmp_path, 18, 1)
  route = partial_inventory(tmp_path, name, permitted=lambda: True)
  consumed = []
  with pytest.raises(RouteInputCancelled, match='cancelled'):
    walk_partial_route(tmp_path, route, permitted=lambda: not consumed,
                       consume=lambda number, events: consumed.append(number))
  assert consumed == [0]
