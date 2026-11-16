import http.client
import json
from pathlib import Path
import threading

from openpilot.starpilot.galaxy.access import GalaxyAccessOwner
from openpilot.starpilot.galaxy.server import make_server


def test_historical_review_session_origin_and_shutdown(tmp_path):
  queued = []
  closed = []

  class Reviews:
    def snapshot(self):
      return {'version': 1, 'reviews': []}
    def request(self, source, route_id, *, authorized):
      if not authorized():
        raise PermissionError
      queued.append((source, route_id))
      return self.snapshot()
    def close(self):
      closed.append(True)

  class Mode:
    def snapshot(self):
      return {'version': 1, 'mode': 'parked'}
    def select(self, mode, revision, *, authorized):
      if not authorized():
        raise PermissionError
      if revision != 'declared-current':
        raise ValueError('Refresh the learning mode')
      queued.append(('mode', mode))
      return {'version': 1, 'mode': mode}

  server = make_server(port=0, owner=GalaxyAccessOwner(Path(tmp_path)), route_reviews=Reviews(), learning_mode=Mode())
  thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
  thread.start()
  cookie = ''

  def request(path, body=None, *, remote=False, origin=None):
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
    assert request('/api/flm/route-reviews', remote=True)[0] in (401, 503)
    assert request('/api/auth/session')[0] == 200
    assert request('/api/flm/route-reviews')[0] == 200
    assert request('/api/flm/route-reviews', {'source': 'connect', 'routeId': 'declared'})[0] == 202
    assert queued == [('connect', 'declared')]
    assert request('/api/flm/route-reviews', {'source': 'connect', 'routeId': 'declared'}, origin='https://evil.test')[0] == 403
    assert len(queued) == 1
    assert request('/api/flm/route-reviews', {'source': 'connect', 'routeId': 'declared', 'url': 'https://evil.test'})[0] == 400
    assert request('/api/flm/route-reviews?url=bad')[0] == 400
    assert request('/api/flm/mode')[0] == 200
    assert request('/api/flm/mode', {'mode': 'live', 'revision': 'declared-current'})[0] == 200
    assert queued[-1] == ('mode', 'live')
    assert request('/api/flm/mode', {'mode': 'parked', 'revision': 'stale'})[0] == 409
    assert request('/api/flm/mode', {'mode': 'parked', 'revision': 'declared-current'}, origin='https://evil.test')[0] == 403
  finally:
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()
  assert closed == [True]
