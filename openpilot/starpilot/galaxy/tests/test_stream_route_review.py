import hashlib
import json
import os
from pathlib import Path
import select
import signal
import tempfile
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


LOCAL_ROUTE = '00000001--aaaaaaaaaa'


def local_section(root, number, payload=None, *, locked=False):
  folder = root / f'{LOCAL_ROUTE}--{number}'
  folder.mkdir(parents=True, exist_ok=True)
  if payload is not None:
    (folder / 'rlog.zst').write_bytes(payload)
  if locked:
    (folder / 'rlog.lock').touch()
  return folder


def local_worker(tmp_path, calls, *, now=None, allowed=lambda: True, batch=4):
  def analyze(path, codec, *, number, route_id, expected_device, permitted):
    assert expected_device is None and route_id == LOCAL_ROUTE and permitted()
    calls.append(number)
    result = report(path, codec, number=number, route_id=route_id, expected_device=DEVICE, permitted=permitted)
    return result
  return StreamRouteReview(tmp_path / 'cache', permitted=allowed, log_root=tmp_path / 'logs',
                           analyze=analyze, batch_segments=batch, clock=lambda: 100. if now is None else now[0])


def run_local(worker, **kwargs):
  return worker.run(LOCAL_ROUTE, route=worker.local_routes()[LOCAL_ROUTE], **kwargs)


def test_local_restart_append_and_unrelated_qlog_writes_keep_committed_sections(tmp_path):
  root, calls = tmp_path / 'logs', []
  first = local_section(root, 0, b'original-zero')
  local_section(root, 1, b'original-one')
  worker = local_worker(tmp_path, calls, batch=1)
  assert run_local(worker)['reviewedCount'] == 1
  # Shutdown/reboot recreates all Python owners; its SQLite checkpoint remains.
  worker = local_worker(tmp_path, calls, batch=1)
  assert run_local(worker)['reviewedCount'] == 2
  (first / 'qlog.zst').write_bytes(b'independent sparse upload')
  local_section(root, 2, b'original-two')
  final = run_local(worker)
  assert final['wholeRouteReviewed'] and final['reviewedCount'] == 3
  assert calls == [0, 1, 2]
  assert final['counts']['trackingFrames'] == 3 * 201


def test_local_one_file_repair_replaces_only_its_derived_metrics_and_source_digest(tmp_path):
  root, calls = tmp_path / 'logs', []
  paths = [local_section(root, number, f'original-{number}'.encode()) for number in range(3)]
  worker = local_worker(tmp_path, calls)
  before = run_local(worker)
  (paths[1] / 'rlog.zst').write_bytes(b'repaired-longer-original-one')
  after = run_local(local_worker(tmp_path, calls))
  assert calls == [0, 1, 2, 1]
  assert after['wholeRouteReviewed'] and after['reviewedCount'] == 3
  assert after['metrics'][0]['windows'] == 3 and after['counts']['trackingFrames'] == 3 * 201
  assert before['sourceSha256'] != after['sourceSha256']
  fresh = StreamRouteReview(tmp_path / 'fresh', permitted=lambda: True, log_root=root, analyze=worker.analyze)
  assert run_local(fresh)['sourceSha256'] == after['sourceSha256'], 'Scheduling order is not original source identity'


@pytest.mark.parametrize('tail', ['locked', 'empty', 'missing'])
def test_local_open_or_empty_section_does_not_block_other_intact_sources_or_restart(tmp_path, tail):
  root, calls, now = tmp_path / 'logs', [], [100.]
  local_section(root, 0, b'intact-zero')
  blocked = local_section(root, 1, b'incomplete' if tail == 'locked' else b'' if tail == 'empty' else None,
                          locked=tail == 'locked')
  local_section(root, 2, b'intact-two')
  originals = {path.name: path.read_bytes() for path in blocked.iterdir()}
  result = run_local(local_worker(tmp_path, calls, now=now))
  assert calls == [0, 2] and result['reviewedCount'] == 2
  assert result['deferredSegments'] == 1 and not result['allSegmentsAttempted']
  now[0] = 159.
  assert run_local(local_worker(tmp_path, calls, now=now))['deferredSegments'] == 1
  now[0] = 160.
  final = run_local(local_worker(tmp_path, calls, now=now))
  assert calls == [0, 2] and final['allSegmentsAttempted'] and not final['wholeRouteReviewed']
  assert final['reviewedCount'] == 2 and final['omittedCount'] == 1
  assert final['omittedSegments'][0][0] == 1
  assert {path.name: path.read_bytes() for path in blocked.iterdir()} == originals
  # A genuine recorder repair makes only that original section eligible again.
  if (blocked / 'rlog.lock').exists():
    (blocked / 'rlog.lock').unlink()
  (blocked / 'rlog.zst').write_bytes(b'now-independently-closed-one')
  repaired = run_local(local_worker(tmp_path, calls, now=now))
  assert calls == [0, 2, 1] and repaired['wholeRouteReviewed'] and repaired['omittedCount'] == 0
  assert repaired['metrics'][0]['windows'] == 3


def test_local_clock_rollback_cannot_prematurely_omit_an_unfinished_section(tmp_path):
  calls, now = [], [100.]
  local_section(tmp_path / 'logs', 0, locked=True)
  assert run_local(local_worker(tmp_path, calls, now=now))['deferredSegments'] == 1
  now[0] = 10.
  assert run_local(local_worker(tmp_path, calls, now=now))['deferredSegments'] == 1
  now[0] = 69.
  assert run_local(local_worker(tmp_path, calls, now=now))['attemptedSegments'] == 0
  now[0] = 70.
  assert run_local(local_worker(tmp_path, calls, now=now))['omittedCount'] == 1
  assert calls == []


def test_local_permission_loss_keeps_each_committed_section_and_restarts_at_next_source(tmp_path):
  calls, allowed = [], [True]
  for number in range(3):
    local_section(tmp_path / 'logs', number, f'original-{number}'.encode())
  worker = local_worker(tmp_path, calls, allowed=lambda: allowed[0])
  result = run_local(worker, on_progress=lambda value: allowed.__setitem__(0, False))
  assert result['reviewedCount'] == 1 and calls == [0]
  allowed[0] = True
  final = run_local(local_worker(tmp_path, calls, allowed=lambda: allowed[0]))
  assert final['wholeRouteReviewed'] and calls == [0, 1, 2]


def test_local_changed_representative_reconsiders_a_prior_duplicate_omission(tmp_path):
  root, calls = tmp_path / 'logs', []
  first = local_section(root, 0, b'copied-original')
  local_section(root, 1, b'copied-original')
  worker = local_worker(tmp_path, calls)
  before = run_local(worker)
  assert before['reviewedCount'] == before['omittedCount'] == 1 and calls == [0]
  (first / 'rlog.zst').write_bytes(b'new-distinct-original-zero')
  after = run_local(worker)
  assert after['wholeRouteReviewed'] and after['reviewedCount'] == 2
  assert after['omittedCount'] == 0 and calls == [0, 0, 1]


def test_exclusive_cache_owner_survives_process_kill_and_reclaims_only_owned_orphan_spools(tmp_path):
  cache = tmp_path / 'cache'
  calls = []

  def analyze(path, codec, **kwargs):
    calls.append(kwargs['number'])
    return report(path, codec, **kwargs)

  def make(inventory=None):
    return StreamRouteReview(cache, permitted=lambda: True, batch_segments=1,
                             inventory_fetch=inventory or (lambda identity: fixture_rows(2)),
                             download=lambda row, path, permitted: path.write_bytes(str(row['number']).encode()), analyze=analyze)

  worker = make()
  assert worker.run(IDENTITY)['reviewedCount'] == 1 and calls == [0]
  protected = {cache / 'compiled-response.bin': b'native artifact', cache / ('b' * 64 + '-comparison.sqlite3'): b'durable comparison',
               cache / '.connect-inventory-unrecognized.sqlite3': b'not a recognized temporary spool'}
  for path, value in protected.items():
    path.write_bytes(value)
  unexpected = cache / 'section-keepkeep'
  unexpected.mkdir(mode=0o700)
  (unexpected / 'proof.json').write_bytes(b'unexpected content must not be retired')
  outside = tmp_path / 'original-recordings'
  outside.mkdir()
  original = outside / 'rlog.zst'
  original.write_bytes(b'original recorder content must never change')
  directory_link = cache / 'section-linklink'
  directory_link.symlink_to(outside, target_is_directory=True)
  inventory_link = cache / '.connect-inventory-linklink.sqlite3'
  inventory_link.symlink_to(original)
  incoming, outgoing = os.pipe()
  child = os.fork()
  if child == 0:
    os.close(incoming)
    try:
      with worker._exclusive_run():
        section = Path(tempfile.mkdtemp(prefix='section-', dir=cache))
        (section / 'rlog.zst').write_bytes(b'partially downloaded data')
        fd, inventory = tempfile.mkstemp(prefix='.connect-inventory-', suffix='.sqlite3', dir=cache)
        os.write(fd, b'private signed inventory')
        os.close(fd)
        journal = inventory + '-journal'
        fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b'partially committed inventory journal')
        os.close(fd)
        os.write(outgoing, json.dumps({'section': section.name, 'inventory': Path(inventory).name,
                                      'journal': Path(journal).name}).encode())
        os.kill(os.getpid(), signal.SIGSTOP)
    except BaseException:
      os._exit(2)
    os._exit(0)
  os.close(outgoing)
  try:
    assert select.select([incoming], [], [], 5)[0], 'The real owner must acquire its file lock before the contention check'
    names = json.loads(os.read(incoming, 4096))
    assert (cache / names['section'] / 'rlog.zst').read_bytes() == b'partially downloaded data'
    inventory_called = []
    contender = make(inventory=lambda identity: inventory_called.append(True) or fixture_rows(2))
    with pytest.raises(RetryableReview, match='Another route analysis'):
      contender.run(IDENTITY)
    assert not inventory_called, 'Lock acquisition must precede downloads or credential-bearing inventory creation'
    assert all((cache / name).exists() for name in names.values()), 'An active owner retains all of its temporary files'
  finally:
    os.close(incoming)
    os.kill(child, signal.SIGKILL)
    os.waitpid(child, 0)
  # A process-level interruption releases the kernel lock, but leaves disk
  # spools. Restart retires those exact cache entries before resuming section1.
  final = make().run(IDENTITY)
  assert final['wholeRouteReviewed'] and final['reviewedCount'] == 2 and calls == [0, 1]
  assert final['metrics'][0]['windows'] == 2
  assert all(not (cache / name).exists() for name in names.values())
  assert all(path.read_bytes() == value for path, value in protected.items())
  assert (unexpected / 'proof.json').read_bytes() == b'unexpected content must not be retired'
  assert directory_link.is_symlink() and inventory_link.is_symlink()
  assert original.read_bytes() == b'original recorder content must never change'


def test_cache_permissions_are_rechecked_before_reclaim_or_inventory_fetch(tmp_path):
  called = []
  worker = StreamRouteReview(tmp_path / 'cache', permitted=lambda: True,
                             inventory_fetch=lambda identity: called.append(True) or fixture_rows(1))
  worker.store.chmod(0o755)
  try:
    with pytest.raises(ReviewUnavailable, match='ownership changed'):
      worker.run(IDENTITY)
    assert not called
  finally:
    worker.store.chmod(0o700)
