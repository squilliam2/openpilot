import hashlib
from types import SimpleNamespace

import pytest
import requests

from openpilot.starpilot.galaxy.stream_route_review import StreamRouteReview, download_original, original_rows, RetryableReview
from openpilot.starpilot.galaxy.connect_route_review import ReviewUnavailable
from openpilot.starpilot.galaxy.connect_route_stream import fetch_inventory

DEVICE = 'a' * 16
IDENTITY = DEVICE + '|00000001--aaaaaaaaaa'


def fixture_rows(count):
  return [{'number': n, 'filename': 'rlog.zst', 'url': 'https://declared.invalid/not-used'} for n in range(count)]


def report(path, codec, *, number, route_id, expected_device, permitted):
  assert permitted() and expected_device == DEVICE and codec == 'zst'
  return {'routeId': route_id, 'segment': number, 'sourceSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
          'vehicles': ['Declared car'], 'counts': {'trackingFrames': 201}, 'proposalGroups': [],
          'metrics': [{'configurationHash': 'b' * 64, 'domain': 'lateral_acceleration', 'speedRange': 'below_15_mps',
                       'windows': 1, 'samples': 201, 'observedSeconds': 2., 'rmse': .1,
                       'meanAbsoluteError': .08, 'worstAbsoluteError': .2, 'errorReversalHz': .5}]}


@pytest.mark.parametrize('count', [51, 355, 513])
def test_every_segment_contributes_beyond_old_caps_and_resume_does_not_repeat(tmp_path, count):
  downloads = []
  def download(row, path, *, permitted):
    downloads.append(row['number'])
    path.write_bytes(str(row['number']).encode())
  worker = StreamRouteReview(tmp_path, permitted=lambda: True, inventory_fetch=lambda identity: fixture_rows(count),
                             download=download, analyze=report, batch_segments=37)
  result = worker.run(IDENTITY)
  assert result['attemptedSegments'] == 37 and result['state'] == 'queued'
  worker = StreamRouteReview(tmp_path, permitted=lambda: True, inventory_fetch=lambda identity: fixture_rows(count),
                             download=download, analyze=report, batch_segments=37)
  while not result['allSegmentsAttempted']:
    result = worker.run(IDENTITY)
  assert downloads == list(range(count))
  assert result['wholeRouteReviewed'] and result['reviewedCount'] == count
  assert result['metrics'][0]['windows'] == count and result['counts']['trackingFrames'] == count * 201
  assert len(list(worker.iter_segments(IDENTITY))) == count
  assert len(result['reviewedSegments']) == 32 and result['segmentPreviewTruncated']
  assert not list(tmp_path.glob('section-*'))


def test_omitted_tail_is_not_a_route_wide_failure_and_copies_are_not_evidence(tmp_path):
  def download(row, path, *, permitted):
    path.write_bytes(str(row['number'] if row['number'] != 1 else 0).encode())
  def analyze(path, codec, **kwargs):
    if kwargs['number'] == 3:
      raise ReviewUnavailable('Original compressed segment is incomplete')
    return report(path, codec, **kwargs)
  worker = StreamRouteReview(tmp_path, permitted=lambda: True, inventory_fetch=lambda identity: fixture_rows(4),
                             download=download, analyze=analyze)
  result = worker.run(IDENTITY)
  assert result['attemptedSegments'] == 4 and result['reviewedCount'] == result['omittedCount'] == 2
  assert result['allSegmentsAttempted'] and not result['wholeRouteReviewed']
  assert [row[0] for row in result['omittedSegments']] == [1, 3]
  assert result['metrics'][0]['windows'] == 2


def test_ignition_pause_keeps_completed_sections_and_resumes_remaining(tmp_path):
  allowed = [True]
  downloads = []
  def download(row, path, *, permitted):
    downloads.append(row['number'])
    path.write_bytes(str(row['number']).encode())
  worker = StreamRouteReview(tmp_path, permitted=lambda: allowed[0], inventory_fetch=lambda identity: fixture_rows(5),
                             download=download, analyze=report)
  first = worker.run(IDENTITY, on_progress=lambda result: allowed.__setitem__(0, False))
  assert first['attemptedSegments'] == 1 and first['state'] == 'queued'
  allowed[0] = True
  after = worker.run(IDENTITY)
  assert after['wholeRouteReviewed'] and downloads == [0, 1, 2, 3, 4]


def test_inventory_has_no_segment_count_cap_but_still_binds_device_and_storage():
  urls = [f'https://commadata2.blob.core.windows.net/data/{DEVICE}/00000001--aaaaaaaaaa/{n}/rlog.zst?sig=private' for n in range(1001)]
  assert len(original_rows({'rlogs': urls}, IDENTITY)) == 1001
  for bad in (urls[0].replace(DEVICE, 'b' * 16), urls[0].replace('commadata2.blob.core.windows.net', 'example.invalid')):
    with pytest.raises(ReviewUnavailable):
      original_rows({'rlogs': [bad]}, IDENTITY)
  with pytest.raises(ReviewUnavailable):
    original_rows({'rlogs': [urls[0], urls[0]]}, IDENTITY)


def test_source_inventory_change_requires_explicit_restart(tmp_path):
  rows = fixture_rows(2)
  worker = StreamRouteReview(tmp_path, permitted=lambda: True, inventory_fetch=lambda identity: rows,
                             download=lambda row, path, permitted: path.write_bytes(str(row['number']).encode()),
                             analyze=report, batch_segments=1)
  worker.run(IDENTITY)
  rows.append(fixture_rows(3)[-1])
  with pytest.raises(ReviewUnavailable, match='inventory changed'):
    worker.run(IDENTITY)
  worker.reset(IDENTITY)
  assert worker.run(IDENTITY)['attemptedSegments'] == 1


def test_local_inventory_does_not_truncate_long_routes(tmp_path):
  root = tmp_path / 'logs'
  for number in range(513):
    folder = root / f'00000001--aaaaaaaaaa--{number}'
    folder.mkdir(parents=True)
    (folder / 'rlog.zst').write_bytes(b'fixture')
  worker = StreamRouteReview(tmp_path / 'cache', permitted=lambda: True, log_root=root)
  route_id = '00000001--aaaaaaaaaa'
  route = worker.local_routes()[route_id]
  assert len(route['segments']) == 513
  rows = worker._rows(route_id, route)
  assert not isinstance(rows, list) and len(rows) == 513
  assert [row['number'] for row in rows] == list(range(513))
  assert sum(1 for _ in rows) == 513
  rows.close()
  assert not list(worker.store.glob('.connect-inventory-*'))


def test_transient_download_retry_preserves_successes_without_counting_failed_attempt(tmp_path):
  attempts, failed = [], [False]
  def download(row, path, *, permitted):
    attempts.append(row['number'])
    path.write_bytes(str(row['number']).encode())
    if row['number'] == 2 and not failed[0]:
      failed[0] = True
      raise RetryableReview('Temporary storage failure; retry later')
  worker = StreamRouteReview(tmp_path, permitted=lambda: True, inventory_fetch=lambda identity: fixture_rows(4),
                             download=download, analyze=report)
  first = worker.run(IDENTITY)
  assert first['state'] == 'queued' and first['attemptedSegments'] == first['reviewedCount'] == 2
  assert first['omittedCount'] == 0 and first['metrics'][0]['windows'] == 2
  assert 'Temporary' in first['retryReason'] and not list(tmp_path.glob('section-*'))
  worker = StreamRouteReview(tmp_path, permitted=lambda: True, inventory_fetch=lambda identity: fixture_rows(4),
                             download=download, analyze=report)
  final = worker.run(IDENTITY)
  assert final['wholeRouteReviewed'] and final['attemptedSegments'] == 4
  assert final['omittedCount'] == 0 and final['metrics'][0]['windows'] == 4
  assert final['counts']['trackingFrames'] == 4 * 201 and final['retryReason'] is None
  assert attempts == [0, 1, 2, 2, 3]


@pytest.mark.parametrize('status', [408, 429, 500, 503])
def test_inventory_transient_http_response_is_retryable_and_closed(tmp_path, status):
  class Response:
    status_code = status
    closed = False
    def __enter__(self):
      return self
    def __exit__(self, *args):
      self.closed = True
  response = Response()
  with pytest.raises(RetryableReview, match='temporarily'):
    fetch_inventory(IDENTITY, tmp_path, get=lambda *args, **kwargs: response,
                    api_factory=lambda device: SimpleNamespace(get_token=lambda: 'declared-token'),
                    provider=lambda: SimpleNamespace(name='comma'))
  assert response.closed and not list(tmp_path.iterdir())


def test_inventory_network_error_is_retryable_and_does_not_expose_credentials(tmp_path):
  def timeout(*args, **kwargs):
    raise requests.Timeout('declared-sensitive-token')
  with pytest.raises(RetryableReview) as caught:
    fetch_inventory(IDENTITY, tmp_path, get=timeout,
                    api_factory=lambda device: SimpleNamespace(get_token=lambda: 'declared-token'),
                    provider=lambda: SimpleNamespace(name='comma'))
  assert 'sensitive' not in str(caught.value) and not list(tmp_path.iterdir())


def test_storage_network_error_is_retryable_and_does_not_expose_signed_url(tmp_path, monkeypatch):
  def timeout(*args, **kwargs):
    raise requests.Timeout('declared-sensitive-sas')
  monkeypatch.setattr(requests, 'get', timeout)
  with pytest.raises(RetryableReview) as caught:
    download_original(fixture_rows(1)[0], tmp_path / 'rlog.zst', permitted=lambda: True)
  assert 'sensitive' not in str(caught.value)


def test_local_open_lock_is_retryable_even_before_dense_file_exists(tmp_path):
  folder = tmp_path / 'recording'
  folder.mkdir()
  (folder / 'rlog.lock').touch()
  with pytest.raises(RetryableReview, match='still open'):
    download_original({'local': str(folder / 'rlog.zst')}, tmp_path / 'copy.zst', permitted=lambda: True)
  assert not (tmp_path / 'copy.zst').exists()


def test_local_inventory_spool_is_removed_when_identity_validation_fails(tmp_path):
  worker = StreamRouteReview(tmp_path / 'cache', permitted=lambda: True, log_root=tmp_path / 'logs')
  route_id = '00000001--aaaaaaaaaa'
  route = {'routeId': route_id, 'segments': [{'number': 3, 'segmentName': route_id + '--2'}]}
  with pytest.raises(ReviewUnavailable, match='identity changed'):
    worker._rows(route_id, route)
  assert not list(worker.store.glob('.connect-inventory-*'))
