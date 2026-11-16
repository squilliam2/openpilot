"""Production route pipeline queues intent only after the complete comparison."""

import hashlib
import http.client
import json
from pathlib import Path
import threading

import pytest

from openpilot.common.params import Params
from openpilot.starpilot.flm.route_tuning_import import snapshot as import_snapshot
from openpilot.starpilot.galaxy.access import GalaxyAccessOwner
from openpilot.starpilot.galaxy.route_reviews import RouteReviews
from openpilot.starpilot.galaxy.server import make_server
from openpilot.starpilot.galaxy.stream_route_review import StreamRouteReview

DEVICE = 'a' * 16
ROUTE = '00000001--aaaaaaaaaa'
IDENTITY = DEVICE + '|' + ROUTE


def pipeline(tmp_path, monkeypatch, *, count=51, disagree=False):
  params = Params(str(tmp_path / 'params'))
  root = tmp_path / 'logs'
  root.mkdir()
  downloads = []

  def download(row, path, *, permitted):
    assert permitted()
    downloads.append(row['number'])
    path.write_bytes(str(row['number']).encode())

  def analyze(path, codec, *, number, route_id, expected_device, permitted):
    assert codec == 'zst' and route_id == IDENTITY and expected_device == DEVICE and permitted()
    return {'routeId': route_id, 'segment': number, 'sourceSha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'vehicleIdentity': {'fingerprint': 'declared-car', 'vin': '0' * 17,
                                'firmwareSha256': 'b' * 64, 'deviceId': DEVICE},
            'vehicles': ['Declared car'], 'counts': {'trackingFrames': 201}, 'metrics': [],
            'proposalGroups': [{'configurationHash': 'c' * 64, 'cohort': 'high:left:steady', 'windows': 4,
                                'proposals': [{'parameter': 'ff_gain_left',
                                               'delta': -.005 if disagree and number % 2 else .005}]}]}

  stream = StreamRouteReview(tmp_path / 'stream', permitted=lambda: True, log_root=root, device_id=lambda: DEVICE,
                             inventory_fetch=lambda identity: [{'number': number, 'filename': 'rlog.zst'} for number in range(count)],
                             download=download, analyze=analyze, batch_segments=4)
  owner = RouteReviews(root, tmp_path / 'cache', params=params, stream=stream,
                       permitted=lambda: True, device_id=lambda: DEVICE)
  monkeypatch.setattr(owner, 'start', lambda: None)
  return params, root, owner, downloads


def finish(owner):
  for _ in range(100):
    assert owner.run_once()
    row = owner.snapshot()['reviews'][0]
    if row['status'] in ('ready', 'failed'):
      return row
  pytest.fail('Fixture processing did not finish')


def test_full_stream_waits_for_every_section_then_queues_one_native_trial_intent(tmp_path, monkeypatch):
  params, root, owner, downloads = pipeline(tmp_path, monkeypatch)
  before = {key: params.get(key) for key in ('AutomaticSteeringProfiles', 'AutomaticSteeringLiveTrials')}
  owner.request('connect', ROUTE, authorized=lambda: True, purpose='tune')
  assert owner.run_once()
  partial = owner.snapshot()['reviews'][0]
  assert partial['status'] == 'queued' and partial['summary']['attemptedSegments'] == 4
  assert import_snapshot(params) == []
  final = finish(owner)
  assert final['status'] == 'ready' and final['summary']['reviewedCount'] == 51
  assert final['summary']['allSegmentsAttempted'] and final['summary']['segmentPreviewTruncated']
  assert downloads == list(range(51)) and not list(root.iterdir())
  selected = final['summary']['tuning']['selected']
  assert selected['training']['support'] == 26 and selected['heldout']['support'] == 25
  assert selected['parameter'] == 'ff_gain_left' and not selected['nativeQualified']
  jobs = import_snapshot(params)
  assert len(jobs) == 1 and jobs[0]['state'] == 'waiting_for_vehicle'
  assert final['summary']['application']['intentId'] == jobs[0]['intentId']
  assert before == {key: params.get(key) for key in before}
  assert not final['summary']['tuneChanged'] and not final['summary']['automaticApplicationAllowed']
  assert not owner.run_once()
  assert len(import_snapshot(params)) == 1


@pytest.mark.parametrize('purpose,disagree', [('review', False), ('tune', True)])
def test_analysis_only_or_rejected_heldout_evidence_never_queues_a_tune(tmp_path, monkeypatch, purpose, disagree):
  params, _, owner, downloads = pipeline(tmp_path, monkeypatch, count=8, disagree=disagree)
  owner.request('connect', ROUTE, authorized=lambda: True, purpose=purpose)
  final = finish(owner)
  assert final['status'] == 'ready' and final['summary']['allSegmentsAttempted']
  assert downloads == list(range(8)) and import_snapshot(params) == []
  if purpose == 'review':
    assert final['summary']['analysisOnly'] and 'tuning' not in final['summary']
  else:
    assert final['summary']['tuning']['selected'] is None
    assert final['summary']['tuning']['reason'] == 'heldout_sections_do_not_support_training_selected_direction'
  assert params.get('AutomaticSteeringProfiles') is None and params.get('AutomaticSteeringLiveTrials') is None


def test_http_new_purpose_and_legacy_body_keep_authentication_and_origin_checks(tmp_path):
  queued = []

  class Reviews:
    def snapshot(self):
      return {'version': 1, 'reviews': []}

    def request(self, source, route_id, *, authorized, purpose='review'):
      if not authorized():
        raise PermissionError
      if purpose not in ('tune', 'review'):
        raise ValueError('Choose a review purpose')
      queued.append((source, route_id, purpose))
      return self.snapshot()

    def close(self):
      pass

  server = make_server(port=0, owner=GalaxyAccessOwner(Path(tmp_path)), route_reviews=Reviews())
  thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
  thread.start()
  cookie = ''

  def request(path, body=None, *, origin=None, remote=False):
    nonlocal cookie
    headers = {'Origin': origin or f'http://127.0.0.1:{server.server_port}', 'Content-Type': 'application/json'}
    if cookie:
      headers['Cookie'] = cookie
    if remote:
      headers['Forwarded'] = 'for=203.0.113.8'
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
      connection.request('GET' if body is None else 'POST', path, json.dumps(body) if body is not None else None, headers)
      response = connection.getresponse()
      if response.getheader('Set-Cookie'):
        cookie = response.getheader('Set-Cookie').split(';', 1)[0]
      return response.status, json.loads(response.read())
    finally:
      connection.close()

  try:
    tune = {'source': 'connect', 'routeId': ROUTE, 'purpose': 'tune'}
    assert request('/api/flm/route-reviews', tune, remote=True)[0] in (401, 503)
    assert not queued
    assert request('/api/auth/session')[0] == 200
    assert request('/api/flm/route-reviews', tune)[0] == 202
    assert request('/api/flm/route-reviews', {'source': 'connect', 'routeId': ROUTE})[0] == 202
    assert queued == [('connect', ROUTE, 'tune'), ('connect', ROUTE, 'review')]
    assert request('/api/flm/route-reviews', {**tune, 'purpose': 'unsafe'})[0] == 400
    assert request('/api/flm/route-reviews', tune, origin='https://evil.test')[0] == 403
    assert request('/api/flm/route-reviews', {**tune, 'applyNow': True})[0] == 400
    assert len(queued) == 2
  finally:
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()
