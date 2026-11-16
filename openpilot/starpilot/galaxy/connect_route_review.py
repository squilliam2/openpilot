"""Explicit, bounded Connect rlog fetches into a private review-only directory."""

import os
from pathlib import Path
import re
import shutil
from urllib.parse import quote, unquote, urlsplit

from openpilot.starpilot.flm.historical_review import selected_rows

ROUTE = re.compile(r'(?:[0-9a-f]{8}--[0-9a-f]{10}|[0-9]{4}-[0-9]{2}-[0-9]{2}--[0-9]{2}-[0-9]{2}-[0-9]{2})\Z')
DEVICE = re.compile(r'[0-9a-f]{16}\Z')
MAX_BYTES = 14 * 32 * 1024 * 1024


class ReviewUnavailable(ValueError):
  """Only fixed, user-facing failure messages; never a URL or credential."""


class RetryableReview(ReviewUnavailable):
  """A temporary transfer failure must preserve completed review checkpoints."""


def parse_route(value, device):
  if type(value) is not str or not 1 <= len(value) <= 512 or not DEVICE.fullmatch(device or ''):
    raise ReviewUnavailable('Enter a Connect route for this registered comma')
  value = value.strip()
  if value.startswith('https://'):
    url = urlsplit(value)
    if url.hostname != 'connect.comma.ai' or url.username or url.password or url.port not in (None, 443):
      raise ReviewUnavailable('Use a connect.comma.ai route URL')
    value = unquote(url.path).strip('/')
  parts = value.replace('|', '/').split('/')
  if len(parts) == 1:
    parts.insert(0, device)
  if len(parts) != 2 or parts[0] != device or not ROUTE.fullmatch(parts[1]):
    raise ReviewUnavailable('Choose a route belonging to this comma device')
  return parts[0] + '|' + parts[1]


def original_urls(value, identity):
  """Trust only API-selected original rlogs at comma's HTTPS storage host."""
  if type(value) is not dict:
    raise ReviewUnavailable('Connect did not return a route inventory')
  device, route = identity.split('|')
  found = {}
  count = 0
  for urls in value.values():
    if type(urls) is not list:
      continue
    for text in urls:
      count += 1
      if count > 4096 or type(text) is not str or len(text) > 8192:
        raise ReviewUnavailable('Connect route inventory exceeds the review bound')
      url = urlsplit(text)
      path = unquote(url.path).split('/')
      if len(path) < 3 or path[-1] not in ('rlog.zst', 'rlog.bz2'):
        continue
      if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443) or
          re.fullmatch(r'commadata[a-z0-9-]*\.blob\.core\.windows\.net', url.hostname or '') is None or
          f'/{device}/{route}/' not in unquote(url.path) or not path[-2].isdigit()):
        raise ReviewUnavailable('Connect returned an unexpected original-log location')
      number = int(path[-2])
      if not 0 <= number < 512 or number in found:
        raise ReviewUnavailable('Connect original segment identity is ambiguous or too large')
      found[number] = {'number': number, 'url': text, 'filename': path[-1]}
  if not found:
    raise ReviewUnavailable('No original rlogs are available for this Connect route')
  return [found[n] for n in sorted(found)]


def fetch(identity, root, *, permitted, inventory=None, get=None):
  """No automatic cloud discovery, no arbitrary URLs, no auth sent to storage."""
  import requests
  if not permitted():
    raise InterruptedError('Connect review waits until parked')
  device, _ = identity.split('|')
  if inventory is None:
    from openpilot.common.api import Api
    from openpilot.starpilot.connect.provider import active_provider

    if active_provider().name != 'comma':
      raise ReviewUnavailable('Select comma as the cloud provider to review Connect routes')
    api = Api(device)
    response = api.get('v1/route/' + quote(identity, safe='') + '/files', timeout=15, access_token=api.get_token())
    if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
      raise ReviewUnavailable('Connect route is unavailable or access was refused')
    inventory = response.json()
  rows = original_urls(inventory, identity)
  root = Path(root)
  root.mkdir(mode=0o700, parents=True, exist_ok=True)
  if root.is_symlink() or not root.is_dir():
    raise ReviewUnavailable('Review download directory is unavailable')
  if shutil.disk_usage(root).free < MAX_BYTES + 64 * 1024 * 1024:
    raise ReviewUnavailable('Free at least 512 MiB of storage for a Connect review')
  downloaded, total = [], 0
  get = requests.get if get is None else get
  try:
    for row in selected_rows(rows):
      if not permitted():
        raise InterruptedError('Connect download cancelled')
      folder = root / (identity.replace('|', '_') + '--' + str(row['number']))
      folder.mkdir(mode=0o700)
      path = folder / row['filename']
      with get(row['url'], stream=True, timeout=(5, 5), allow_redirects=False) as response:
        if response.status_code != 200:
          raise ReviewUnavailable('An original Connect segment could not be downloaded')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        size = 0
        with os.fdopen(os.open(path, flags, 0o600), 'wb') as target:
          for chunk in response.iter_content(256 * 1024):
            if not permitted():
              raise InterruptedError('Connect download cancelled')
            size += len(chunk)
            total += len(chunk)
            if size > 32 * 1024 * 1024 or total > MAX_BYTES:
              raise ReviewUnavailable('Connect review download exceeds the bounded storage limit')
            target.write(chunk)
        if not size:
          raise ReviewUnavailable('Connect returned an empty original segment')
      downloaded.append({'number': row['number'], 'segmentName': folder.name, 'files': {'rlog': True}})
    return {'routeId': identity, 'segmentCount': len(downloaded), 'segments': downloaded}, len(rows)
  except requests.RequestException:
    raise ReviewUnavailable('Connect download failed; check Wi-Fi and route availability') from None
