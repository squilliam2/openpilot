"""Connect inventory streaming uses injected HTTP and disposable private spools."""

import json
import os
from types import SimpleNamespace

import pytest

from openpilot.starpilot.galaxy.connect_route_review import ReviewUnavailable
from openpilot.starpilot.galaxy.connect_route_stream import CHUNK_BYTES, fetch_inventory, spool_inventory

IDENTITY = 'a' * 16 + '|00000001--aaaaaaaaaa'


def url(number, *, host='commadata2.blob.core.windows.net', filename='rlog.zst'):
  return f'https://{host}/' + IDENTITY.replace('|', '/') + f'/{number}/{filename}?sig=declared-private-sas'


def chunks(raw, size=37):
  return (raw[index:index + size] for index in range(0, len(raw), size))


def test_inventory_above_50_and_512_has_no_count_cap_and_replays_sorted_rows(tmp_path):
  raw = json.dumps({'rlogs': [url(number) for number in reversed(range(1001))], 'qlogs': [url(0, filename='qlog.zst')]}).encode()
  rows = spool_inventory(IDENTITY, tmp_path, chunks(raw))
  path = rows.path
  assert len(rows) == 1001 and os.stat(path).st_mode & 0o777 == 0o600
  assert [row['number'] for row in rows] == list(range(1001))
  assert sum(1 for _ in rows) == 1001
  rows.close()
  assert not path.exists() and not list(tmp_path.iterdir())
  rows.close()
  with pytest.raises(ReviewUnavailable, match='closed'):
    list(rows)


def test_splits_inside_escapes_and_utf8_preserve_original_url(tmp_path):
  raw = json.dumps({'notes': 'déclared', 'rlogs': [url(3)]}, ensure_ascii=False).replace('/', '\\/').replace('https', 'h\\u0074tps').encode()
  with spool_inventory(IDENTITY, tmp_path, chunks(raw, 1)) as rows:
    assert list(rows) == [{'number': 3, 'url': url(3), 'filename': 'rlog.zst'}]
  assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('raw', [b'{"rlogs":[', b'{"rlogs":["unfinished', b'{"rlogs":[]',
                                b'{"rlogs":[],}', b'{"rlogs":[1]}', b'{"rlogs":{}}',
                                b'{"rlogs":[]}garbage', b'{"rlogs":["\\q"]}',
                                b'{"rlogs":null,"rlogs":[]}', b'{"rlogs":["\xff"]}',
                                b'{"count":1', b'{"count":nul', b'{"count":1e}'])
def test_invalid_or_incomplete_json_removes_private_spool(tmp_path, raw):
  with pytest.raises(ReviewUnavailable):
    spool_inventory(IDENTITY, tmp_path, chunks(raw, 3))
  assert not list(tmp_path.iterdir())


def test_numeric_metadata_split_inside_exponent_and_signed_fraction(tmp_path):
  raw = ('{"first":-1.05e+2,"second":true,"third":null,"rlogs":[' + json.dumps(url(1)) + ']}').encode()
  with spool_inventory(IDENTITY, tmp_path, chunks(raw, 1)) as rows:
    assert len(rows) == 1


@pytest.mark.parametrize('bad', [lambda: url(1, host='evil.example'),
                                lambda: url(1).replace('https:', 'http:'),
                                lambda: url(1).replace('https://', 'https://user:secret@'),
                                lambda: url(1).replace('00000001--aaaaaaaaaa', '00000002--bbbbbbbbbb'),
                                lambda: url(2**32), lambda: url(1) + '#fragment'])
def test_unexpected_original_locations_are_refused_without_disclosing_url(tmp_path, bad):
  value = bad()
  with pytest.raises(ReviewUnavailable) as caught:
    spool_inventory(IDENTITY, tmp_path, chunks(json.dumps({'rlogs': [value]}).encode()))
  assert 'declared-private-sas' not in str(caught.value) and 'secret' not in str(caught.value)
  assert not list(tmp_path.iterdir())


def test_duplicate_original_index_across_arrays_is_rejected(tmp_path):
  with pytest.raises(ReviewUnavailable, match='duplicate original'):
    spool_inventory(IDENTITY, tmp_path, chunks(json.dumps({'rlogs': [url(1)], 'other': [url(1, filename='rlog.bz2')]}).encode()))
  assert not list(tmp_path.iterdir())


def test_cancelled_inventory_does_not_leave_signed_urls_or_partial_checkpoint(tmp_path):
  count = [0]
  def allowed():
    count[0] += 1
    return count[0] < 5
  with pytest.raises(InterruptedError):
    spool_inventory(IDENTITY, tmp_path, chunks(json.dumps({'rlogs': [url(number) for number in range(100)]}).encode()), permitted=allowed)
  assert not list(tmp_path.iterdir())


def test_string_budget_and_chunk_budget_do_not_become_a_route_count_limit(tmp_path):
  with pytest.raises(ReviewUnavailable, match='string'):
    spool_inventory(IDENTITY, tmp_path, chunks(json.dumps({'rlogs': ['x' * 8193]}).encode()))
  with pytest.raises(ReviewUnavailable, match='chunk'):
    spool_inventory(IDENTITY, tmp_path, [b'x' * (CHUNK_BYTES + 1)])
  assert not list(tmp_path.iterdir())


class Response:
  status_code = 200
  def __init__(self, raw):
    self.raw, self.closed = raw, False
  def __enter__(self):
    return self
  def __exit__(self, *args):
    self.closed = True
  @property
  def content(self):
    raise AssertionError('The complete body must never be materialized')
  def json(self):
    raise AssertionError('The complete JSON must never be materialized')
  def iter_content(self, size):
    assert size == CHUNK_BYTES
    return chunks(self.raw, size)


def test_fetch_streams_fixed_comma_api_with_device_jwt_only_there(tmp_path):
  response = Response(json.dumps({'rlogs': [url(1)]}).encode())
  calls = []
  def get(target, **kwargs):
    calls.append((target, kwargs))
    return response
  def api(device):
    assert device == 'a' * 16
    return SimpleNamespace(get_token=lambda: 'declared-device-jwt')
  with fetch_inventory(IDENTITY, tmp_path, get=get, api_factory=api, provider=lambda: SimpleNamespace(name='comma')) as rows:
    assert len(rows) == 1
  target, kwargs = calls[0]
  assert target == 'https://api.commadotai.com/v1/route/' + IDENTITY.replace('|', '%7C') + '/files'
  assert kwargs['stream'] is True and kwargs['allow_redirects'] is False
  assert kwargs['headers']['Authorization'] == 'JWT declared-device-jwt'
  assert response.closed and not list(tmp_path.iterdir())


def test_other_provider_and_unexpected_identity_never_fetch_or_mint_token(tmp_path):
  def denied(*args, **kwargs):
    raise AssertionError('No network or credentials may be touched')
  with pytest.raises(ReviewUnavailable, match='provider'):
    fetch_inventory(IDENTITY, tmp_path, get=denied, api_factory=denied, provider=lambda: SimpleNamespace(name='offline'))
  with pytest.raises(ReviewUnavailable, match='identity'):
    fetch_inventory('bad', tmp_path, get=denied, api_factory=denied)
  assert not list(tmp_path.iterdir())


def test_private_directory_rejects_symlink_and_does_not_remove_foreign_files(tmp_path):
  root = tmp_path / 'private'
  root.mkdir()
  linked = tmp_path / 'link'
  linked.symlink_to(root, target_is_directory=True)
  with pytest.raises(ReviewUnavailable, match='storage'):
    spool_inventory(IDENTITY, linked, chunks(json.dumps({'rlogs': [url(1)]}).encode()))
  assert root.exists() and not list(root.iterdir())
