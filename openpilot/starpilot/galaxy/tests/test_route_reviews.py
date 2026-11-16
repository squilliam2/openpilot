from unittest.mock import Mock

import pytest

from openpilot.starpilot.galaxy.connect_route_review import parse_route, original_urls, fetch
from openpilot.starpilot.galaxy.route_reviews import RouteReviews
from openpilot.starpilot.flm.tests.test_historical_review import ROUTE, recording

DEVICE = 'a' * 16


def inventory(count=1):
  return {'logs': [f'https://commadata2.blob.core.windows.net/data/{DEVICE}/{ROUTE}/{n}/rlog.zst?sig=private' for n in range(count)]}


def test_connect_input_does_not_accept_arbitrary_urls_or_another_device():
  assert parse_route(ROUTE, DEVICE) == DEVICE + '|' + ROUTE
  assert parse_route(f'https://connect.comma.ai/{DEVICE}/{ROUTE}?seek=10', DEVICE) == DEVICE + '|' + ROUTE
  assert parse_route(DEVICE + '/' + ROUTE, DEVICE) == DEVICE + '|' + ROUTE
  for text in ('http://127.0.0.1/private', f'https://evil.test/{DEVICE}/{ROUTE}', f'https://connect.comma.ai/other/{ROUTE}', '../secret'):
    with pytest.raises(ValueError):
      parse_route(text, DEVICE)
  for url in ('http://commadata2.blob.core.windows.net', 'https://127.0.0.1', 'https://commadata2.blob.core.windows.net.evil.test'):
    with pytest.raises(ValueError):
      original_urls({'logs': [f'{url}/data/{DEVICE}/{ROUTE}/0/rlog.zst']}, DEVICE + '|' + ROUTE)
  with pytest.raises(ValueError):
    original_urls({'logs': inventory()['logs'] * 2}, DEVICE + '|' + ROUTE)


def test_connect_download_is_explicit_bounded_and_never_sends_auth_to_storage(tmp_path):
  class Response:
    status_code = 200
    def __enter__(self):
      return self
    def __exit__(self, *args):
      pass
    def iter_content(self, *args):
      yield b'declared fixture bytes; decoder is tested separately'
  get = Mock(return_value=Response())
  route, available = fetch(DEVICE + '|' + ROUTE, tmp_path, permitted=lambda: True, inventory=inventory(355), get=get)
  assert available == 355 and len(route['segments']) == 14 and get.call_count == 14
  assert all(call.kwargs == {'stream': True, 'timeout': (5, 5), 'allow_redirects': False} for call in get.call_args_list)
  assert all((tmp_path / r['segmentName'] / 'rlog.zst').exists() for r in route['segments'])
  blocked = Mock()
  with pytest.raises(InterruptedError):
    fetch(DEVICE + '|' + ROUTE, tmp_path / 'blocked', permitted=lambda: False, inventory=inventory(), get=blocked)
  blocked.assert_not_called()


def test_automatic_local_backfill_reviews_older_than_eight_and_survives_restart(tmp_path):
  from openpilot.starpilot.flm.historical_review import review
  root = tmp_path / 'logs'
  recording(root)
  old = root / ('00000020--bbbbbbbbbb--0')
  old.mkdir()
  (old / 'rlog.zst').write_bytes((root / (ROUTE + '--0') / 'rlog.zst').read_bytes())
  analyzer = Mock(side_effect=review)
  downloads = Mock(side_effect=AssertionError('Local backfill must not access Connect'))
  owner = RouteReviews(root, tmp_path / 'cache', permitted=lambda: True, analyzer=analyzer, downloader=downloads)
  assert owner.run_once() and owner.run_once()
  assert {r['status'] for r in owner.snapshot()['reviews']} == {'ready'}
  assert not owner.run_once()
  restarted = RouteReviews(root, tmp_path / 'cache', permitted=lambda: True, analyzer=analyzer, downloader=downloads)
  assert len(restarted.snapshot()['reviews']) == 2 and not restarted.run_once()
  assert analyzer.call_count == 2
  downloads.assert_not_called()
  assert not list(root.glob('**/*Profiles*'))


def test_cloud_request_waits_for_parked_and_cannot_change_logger_or_tune(tmp_path, monkeypatch):
  root = tmp_path / 'logs'
  root.mkdir()
  allowed = [False]
  downloader = Mock(side_effect=ValueError('Connect route is unavailable'))
  owner = RouteReviews(root, tmp_path / 'cache', permitted=lambda: allowed[0], device_id=lambda: DEVICE, downloader=downloader)
  monkeypatch.setattr(owner, 'start', lambda: None)
  with pytest.raises(PermissionError):
    owner.request('connect', ROUTE, authorized=lambda: False)
  assert not owner.snapshot()['reviews']
  owner.request('connect', ROUTE, authorized=lambda: True)
  assert not owner.run_once()
  downloader.assert_not_called()
  allowed[0] = True
  assert owner.run_once()
  assert downloader.call_count == 1
  assert owner.snapshot()['reviews'][0]['status'] == 'failed'
  assert not list(root.iterdir())  # Import never impersonates a logger-owned route.


def test_cancelled_review_is_queued_and_not_marked_processed(tmp_path):
  root = tmp_path / 'logs'
  recording(root)
  analyzer = Mock(side_effect=InterruptedError('declared ignition transition'))
  owner = RouteReviews(root, tmp_path / 'cache', permitted=lambda: True, analyzer=analyzer)
  assert owner.run_once()
  assert owner.snapshot()['reviews'][0]['status'] == 'queued'


def test_removed_automatic_queue_entries_do_not_block_new_recordings(tmp_path, monkeypatch):
  from openpilot.starpilot.galaxy import route_reviews as module
  monkeypatch.setattr(module, 'MAX_RECORDS', 2)
  root = tmp_path / 'logs'
  recording(root)
  payload = (root / (ROUTE + '--0') / 'rlog.zst').read_bytes()
  old = root / '00000020--bbbbbbbbbb--0'
  old.mkdir()
  (old / 'rlog.zst').write_bytes(payload)
  owner = RouteReviews(root, tmp_path / 'cache', permitted=lambda: True)
  owner.scan()
  assert len(owner.records) == 2 and all(r['status'] == 'queued' for r in owner.records.values())
  (old / 'rlog.zst').unlink()
  old.rmdir()
  newest = root / '000000ff--cccccccccc--0'
  newest.mkdir()
  (newest / 'rlog.zst').write_bytes(payload)
  owner.scan()
  assert set(owner.records) == {'local:' + ROUTE, 'local:000000ff--cccccccccc'}
  assert not owner.snapshot()['scanIncomplete']


def test_cloud_staging_is_removed_after_failure_and_private_urls_are_not_reported(tmp_path, monkeypatch):
  root = tmp_path / 'logs'
  root.mkdir()
  def download(identity, folder, *, permitted):
    from pathlib import Path
    (Path(folder) / 'partial.rlog').write_bytes(b'not a complete log')
    raise ValueError('https://storage.invalid/?sig=DECLARED_SECRET')
  owner = RouteReviews(root, tmp_path / 'cache', permitted=lambda: True, device_id=lambda: DEVICE, downloader=download)
  monkeypatch.setattr(owner, 'start', lambda: None)
  owner.request('connect', ROUTE, authorized=lambda: True)
  assert owner.run_once()
  assert 'DECLARED_SECRET' not in str(owner.snapshot())
  assert not list(owner.cache.directory.glob('connect-review-*'))
  assert not list(root.iterdir())


def test_download_byte_bound_and_no_automatic_redirect(tmp_path, monkeypatch):
  from openpilot.starpilot.galaxy import connect_route_review as module
  class Response:
    status_code = 200
    def __enter__(self):
      return self
    def __exit__(self, *args):
      pass
    def iter_content(self, *args):
      yield b'large declared fixture'
  monkeypatch.setattr(module, 'MAX_BYTES', 3)
  with pytest.raises(ValueError, match='bounded storage'):
    fetch(DEVICE + '|' + ROUTE, tmp_path, permitted=lambda: True, inventory=inventory(), get=Mock(return_value=Response()))


def test_connect_inventory_uses_existing_device_auth_only_for_the_api(tmp_path, monkeypatch):
  from openpilot.common import api as api_module
  from openpilot.starpilot.connect import provider
  from types import SimpleNamespace
  class Response:
    status_code = 200
    def __enter__(self):
      return self
    def __exit__(self, *args):
      pass
    def iter_content(self, *args):
      yield b'declared log bytes'
  api = Mock()
  api.get_token.return_value = 'declared-device-token'
  api.get.return_value.content = b'{}'
  api.get.return_value.status_code = 200
  api.get.return_value.json.return_value = inventory()
  monkeypatch.setattr(api_module, 'Api', Mock(return_value=api))
  monkeypatch.setattr(provider, 'active_provider', lambda: SimpleNamespace(name='comma'))
  get = Mock(return_value=Response())
  fetch(DEVICE + '|' + ROUTE, tmp_path, permitted=lambda: True, get=get)
  api.get.assert_called_once_with('v1/route/' + DEVICE + '%7C' + ROUTE + '/files', timeout=15, access_token='declared-device-token')
  assert 'headers' not in get.call_args.kwargs and 'access_token' not in get.call_args.kwargs
