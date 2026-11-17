"""Local offroad reviews resume their actual stored original-section checkpoints."""

import hashlib
from unittest import mock

import pytest

from openpilot.common.params import Params
from openpilot.starpilot.flm.full_route_review import analyze_segment
from openpilot.starpilot.flm.tests.test_power_loss_recovery import saved_vehicle, closed_section
from openpilot.starpilot.galaxy.route_reviews import RouteReviews
from openpilot.starpilot.galaxy.stream_route_review import StreamRouteReview

DEVICE = 'a' * 16
ROUTE = '00000001--aaaaaaaaaa'
CONNECT = DEVICE + '|00000002--bbbbbbbbbb'


def section(root, number):
  folder = root / f'{ROUTE}--{number}'
  folder.mkdir(parents=True, exist_ok=True)
  (folder / 'rlog.zst').write_bytes(f'original-{number}'.encode())
  return folder


def summary(path, codec, *, number, route_id, expected_device, permitted):
  assert permitted()
  return {'routeId': route_id, 'segment': number, 'sourceSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
          'vehicles': ['Declared car'], 'counts': {'trackingFrames': 1}, 'proposalGroups': [], 'metrics': []}


def owner(tmp_path, calls, *, allowed=lambda: True, batch=1, params=None):
  root = tmp_path / 'logs'
  root.mkdir(exist_ok=True)

  def analyze(path, codec, **kwargs):
    calls.append((kwargs['route_id'], kwargs['number']))
    return summary(path, codec, **kwargs)

  def download(row, path, *, permitted):
    assert permitted()
    path.write_bytes(f"cloud-{row['number']}".encode())

  worker = StreamRouteReview(tmp_path / 'stream', permitted=allowed, log_root=root, batch_segments=batch,
                             inventory_fetch=lambda identity: [{'number': number, 'filename': 'rlog.zst'} for number in range(20)],
                             analyze=analyze, download=download)
  # The production downloader is retained for local sources; injected cloud
  # bytes keep this scheduling fixture independent from network availability.
  from openpilot.starpilot.galaxy.stream_route_review import download_original
  worker.download = lambda row, path, permitted: (download_original(row, path, permitted=permitted) if row.get('local') else
                                                 download(row, path, permitted=permitted))
  result = RouteReviews(root, tmp_path / 'reviews', permitted=allowed, device_id=lambda: DEVICE,
                        params=params, stream=worker, clock=lambda: 100.)
  result.start = mock.Mock(return_value=None)
  return result


def test_local_analysis_starts_from_offroad_tick_without_a_browser_or_manual_request(tmp_path):
  section(tmp_path / 'logs', 0)
  calls = []
  params = Params(str(tmp_path / 'params'))
  review = owner(tmp_path, calls, params=params)
  before = {key: params.get(key) for key in ('CurrentRoute', 'AutomaticSteeringProfiles', 'AutomaticSteeringLiveTrials')}
  assert review.run_once()
  row = review.snapshot()['reviews'][0]
  assert row['source'] == 'local' and row['purpose'] == 'tune' and row['status'] == 'ready'
  assert row['summary']['reviewedCount'] == 1 and calls == [(ROUTE, 0)]
  assert before == {key: params.get(key) for key in before}


def test_local_scan_append_and_explicit_retry_preserve_prior_section_checkpoints_after_restart(tmp_path):
  for number in range(3):
    section(tmp_path / 'logs', number)
  calls = []
  review = owner(tmp_path, calls)
  assert review.run_once() and calls == [(ROUTE, 0)]
  # A retry click while paused must not clear progress.
  review.request('local', ROUTE, authorized=lambda: True)
  restarted = owner(tmp_path, calls)
  section(tmp_path / 'logs', 3)
  restarted.scan()
  while restarted.run_once():
    pass
  final = restarted.snapshot()['reviews'][0]
  assert final['summary']['reviewedCount'] == 4 and final['status'] == 'ready'
  assert calls == [(ROUTE, number) for number in range(4)]


def test_local_and_requested_connect_batches_round_robin_persists_across_restart_and_clock_rollback(tmp_path):
  for number in range(5):
    section(tmp_path / 'logs', number)
  calls = []
  review = owner(tmp_path, calls)
  review.request('connect', CONNECT, authorized=lambda: True)
  assert review.run_once() and calls[-1] == (ROUTE, 0), 'New local drives cannot wait behind the long cloud job'
  assert review.run_once() and calls[-1] == (CONNECT, 0)
  restarted = owner(tmp_path, calls)
  restarted.clock = lambda: 1.  # Scheduling depends on persisted serials, not wall time.
  assert restarted.run_once() and calls[-1] == (ROUTE, 1)
  assert restarted.run_once() and calls[-1] == (CONNECT, 1)
  assert [(name, number) for name, number in calls] == [(ROUTE, 0), (CONNECT, 0), (ROUTE, 1), (CONNECT, 1)]


def test_per_section_progress_survives_permission_loss_inside_a_batch_and_parent_restart(tmp_path):
  for number in range(3):
    section(tmp_path / 'logs', number)
  calls, allowed = [], [True]
  review = owner(tmp_path, calls, allowed=lambda: allowed[0], batch=3)
  save = review._save

  def pause_after_checkpoint():
    save()
    row = review.records.get('local:' + ROUTE, {})
    if (row.get('summary') or {}).get('attemptedSegments') == 1:
      allowed[0] = False

  review._save = pause_after_checkpoint
  assert review.run_once()
  row = review.records['local:' + ROUTE]
  assert row['status'] == 'queued' and row['summary']['reviewedCount'] == 1 and calls == [(ROUTE, 0)]
  assert not review.run_once() and calls == [(ROUTE, 0)], 'An interrupted lease cannot authorize more source processing'
  allowed[0] = True
  restarted = owner(tmp_path, calls, batch=3)
  assert restarted.run_once()
  assert calls == [(ROUTE, 0), (ROUTE, 1), (ROUTE, 2)]
  assert restarted.snapshot()['reviews'][0]['summary']['wholeRouteReviewed']


def test_actual_compressed_local_original_is_not_decoded_again_after_restart_and_append(tmp_path):
  params, cp, scope = saved_vehicle(tmp_path)
  root = tmp_path / 'logs'
  paths = [closed_section(root, cp, scope, number) for number in range(2)]
  originals = {path: path.read_bytes() for path in paths}
  decoded = []

  def analyze(path, codec, **kwargs):
    decoded.append(kwargs['number'])
    return analyze_segment(path, codec, **kwargs)

  def make():
    stream = StreamRouteReview(tmp_path / 'actual-stream', permitted=lambda: True, log_root=root,
                               analyze=analyze, batch_segments=1)
    return RouteReviews(root, tmp_path / 'actual-reviews', permitted=lambda: True, params=params, stream=stream)

  protected = {key: params.get(key) for key in ('CurrentRoute', 'AutomaticSteeringProfiles', 'AutomaticSteeringLiveTrials')}
  first = make()
  assert first.run_once() and decoded == [0]
  paths.append(closed_section(root, cp, scope, 2))
  restarted = make()
  while restarted.run_once():
    pass
  row = restarted.snapshot()['reviews'][0]
  assert decoded == [0, 1, 2] and row['summary']['reviewedCount'] == 3
  assert row['summary']['metrics'] and row['summary']['tuning']['selected'] is None
  assert protected == {key: params.get(key) for key in protected}
  assert all(path.read_bytes() == original for path, original in originals.items())


def test_managed_startup_eagerly_starts_local_reviews_without_http_requests():
  from openpilot.starpilot.galaxy import managed

  server, remote, auth = mock.Mock(), mock.Mock(), mock.Mock()
  with mock.patch.object(managed, 'make_server', return_value=server), \
       mock.patch('openpilot.common.params.Params', return_value=mock.Mock()), \
       mock.patch.object(managed, 'default_remote_pairing', return_value=object()), \
       mock.patch.object(managed, 'make_remote_server', return_value=remote), \
       mock.patch.object(managed, 'make_gateway_auth_server', return_value=auth), \
       mock.patch.object(managed, 'serve_managed') as serve:
    managed.main()
  server.start_drive_history.assert_called_once_with()
  server.start_route_reviews.assert_called_once_with()
  serve.assert_called_once()


@pytest.mark.parametrize('finished_rerun', [False, True])
def test_queueing_during_another_active_cache_lease_defers_reset_and_then_resumes(tmp_path, finished_rerun):
  calls = []
  review = owner(tmp_path, calls)
  route = DEVICE + '|00000003--cccccccccc'
  if finished_rerun:
    review.stream.batch_segments = 100
    old = review.stream.run(route, purpose='review')
    assert old['wholeRouteReviewed'] and old['reviewedCount'] == 20
    review.stream.batch_segments = 1
    review.records['connect:' + route] = {'source': 'connect', 'routeId': route, 'status': 'ready',
                                        'purpose': 'review', 'reason': 'Earlier full review', 'updatedAt': 100., 'summary': old}
    review._save()
    calls.clear()
  # A UI request may queue work while the single numerical/download owner has
  # another route open. It must neither reclaim active spools nor block/reset it.
  with review.stream._exclusive_run():
    review.running = 'connect:' + CONNECT
    value = review.request('connect', route, authorized=lambda: True)
    queued = next(row for row in value['reviews'] if row['routeId'] == route)
    assert queued['status'] == 'queued' and calls == []
  review.running = None
  assert review.run_once() and calls == [(route, 0)]
  assert review.records['connect:' + route]['summary']['reviewedCount'] == 1
  # Clearing the deferred reset must itself persist before processing. A later
  # reboot continues from source1 instead of restarting the requested rerun.
  restarted = owner(tmp_path, calls)
  assert restarted.run_once() and calls == [(route, 0), (route, 1)]
  assert restarted.records['connect:' + route]['summary']['reviewedCount'] == 2
