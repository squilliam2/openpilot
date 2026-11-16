"""Bounded-memory Connect inventory parsing; signed URLs stay in a private spool."""

import codecs
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from urllib.parse import quote, unquote, urlsplit

from openpilot.starpilot.galaxy.connect_route_review import DEVICE, ROUTE, ReviewUnavailable, RetryableReview

CHUNK_BYTES = 64 * 1024
MAX_STRING = 8192
MAX_ENCODED_STRING = 12 * MAX_STRING + 2


def _identity(identity):
  if type(identity) is not str:
    raise ReviewUnavailable('Original Connect route identity required')
  parts = identity.split('|')
  if len(parts) != 2 or not DEVICE.fullmatch(parts[0]) or not ROUTE.fullmatch(parts[1]):
    raise ReviewUnavailable('Original Connect route identity required')
  return parts


def parse_url(value, identity):
  device, route = _identity(identity)
  if type(value) is not str or not 0 < len(value) <= MAX_STRING:
    raise ReviewUnavailable('Connect returned an invalid file location')
  try:
    url = urlsplit(value)
    parts = unquote(url.path).split('/')
    if len(parts) < 3 or parts[-1] not in ('rlog.zst', 'rlog.bz2'):
      return None
    if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443) or url.fragment or
        re.fullmatch(r'commadata[a-z0-9-]*\.blob\.core\.windows\.net', url.hostname or '') is None or
        parts[-4:-2] != [device, route] or re.fullmatch('[0-9]+', parts[-2]) is None):
      raise ReviewUnavailable('Connect returned an unexpected original-log location')
    number = int(parts[-2])
    if number > 2**32 - 1:
      raise ReviewUnavailable('Connect returned an invalid original segment identity')
    return {'number': number, 'url': value, 'filename': parts[-1]}
  except (ValueError, UnicodeError) as error:
    if isinstance(error, ReviewUnavailable):
      raise
    raise ReviewUnavailable('Connect returned an invalid file location') from None


class _JSONInput:
  def __init__(self, chunks, permitted):
    self.chunks, self.permitted = iter(chunks), permitted
    self.buffer, self.at, self.done = '', 0, False
    self.utf8 = codecs.getincrementaldecoder('utf-8')()
    self.decoder = json.JSONDecoder()

  def _fill(self):
    if not self.permitted():
      raise InterruptedError('Connect inventory parsing paused')
    if self.done:
      return False
    try:
      chunk = next(self.chunks)
    except StopIteration:
      self.done = True
      chunk = b''
    if type(chunk) is not bytes or len(chunk) > CHUNK_BYTES:
      raise ReviewUnavailable('Connect inventory chunk exceeds its input budget')
    try:
      text = self.utf8.decode(chunk, final=self.done)
    except UnicodeError:
      raise ReviewUnavailable('Connect returned invalid inventory encoding') from None
    self.buffer, self.at = self.buffer[self.at:] + text, 0
    return bool(text) or not self.done

  def peek(self):
    while self.at == len(self.buffer):
      if not self._fill():
        return None
    return self.buffer[self.at]

  def whitespace(self):
    while self.peek() in (' ', '\n', '\r', '\t'):
      self.at += 1

  def expect(self, character):
    self.whitespace()
    if self.peek() != character:
      raise ReviewUnavailable('Connect returned malformed route inventory')
    self.at += 1

  def string(self, *, maximum=MAX_STRING):
    self.whitespace()
    if self.peek() != '"':
      raise ReviewUnavailable('Connect inventory file lists must contain strings')
    while True:
      try:
        value, end = self.decoder.raw_decode(self.buffer, self.at)
      except json.JSONDecodeError:
        if len(self.buffer) - self.at > MAX_ENCODED_STRING or not self._fill():
          raise ReviewUnavailable('Connect inventory string is invalid or too large') from None
        continue
      if type(value) is not str or len(value) > maximum:
        raise ReviewUnavailable('Connect inventory string is invalid or too large')
      self.at = end
      return value

  def scalar(self):
    self.whitespace()
    if self.peek() == '"':
      return self.string()
    if self.peek() not in ('n', 't', 'f', '-', *'0123456789'):
      raise ReviewUnavailable('Connect inventory must contain flat file lists')
    token = ''
    while self.peek() not in (None, ',', '}', ' ', '\n', '\r', '\t'):
      if len(token) == MAX_STRING:
        raise ReviewUnavailable('Connect inventory scalar is invalid or too large')
      token += self.peek()
      self.at += 1
    try:
      return json.loads(token)
    except json.JSONDecodeError:
      raise ReviewUnavailable('Connect inventory scalar is invalid or too large') from None

  def finish(self):
    self.whitespace()
    if self.peek() is not None:
      raise ReviewUnavailable('Connect inventory contains trailing data')


class InventoryRows:
  """Replayable, sorted rows with O(1) Python memory and explicit URL cleanup."""

  def __init__(self, store):
    store = Path(store)
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = store.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
      raise ReviewUnavailable('Private Connect inventory storage is unavailable')
    fd, name = tempfile.mkstemp(prefix='.connect-inventory-', suffix='.sqlite3', dir=store)
    os.close(fd)
    self.path = Path(name)
    info = self.path.lstat()
    self.identity = info.st_dev, info.st_ino
    self.db = sqlite3.connect(self.path)
    self.db.execute('PRAGMA cache_size=-1024')
    self.db.execute('PRAGMA temp_store=FILE')
    self.db.execute('CREATE TABLE urls(number INTEGER PRIMARY KEY,url TEXT NOT NULL,filename TEXT NOT NULL)')
    self.db.execute('CREATE TABLE fields(name TEXT PRIMARY KEY)')
    self.count, self.closed = 0, False

  def __len__(self):
    return self.count

  def __iter__(self):
    if self.closed:
      raise ReviewUnavailable('Connect inventory spool is closed')
    for number, url, filename in self.db.execute('SELECT number,url,filename FROM urls ORDER BY number'):
      yield {'number': number, 'url': url, 'filename': filename}

  def close(self):
    if not self.closed:
      self.closed = True
      self.db.close()
      try:
        info = self.path.lstat()
        if (info.st_dev, info.st_ino) == self.identity:
          self.path.unlink()
      except FileNotFoundError:
        pass

  def __enter__(self):
    return self

  def __exit__(self, *args):
    self.close()


def spool_inventory(identity, store, chunks, *, permitted=lambda: True):
  """Parse a flat JSON object without materializing any route-length array."""
  _identity(identity)
  rows = InventoryRows(store)
  reader = _JSONInput(chunks, permitted)
  try:
    reader.expect('{')
    reader.whitespace()
    if reader.peek() != '}':
      while True:
        name = reader.string(maximum=256)
        try:
          rows.db.execute('INSERT INTO fields VALUES(?)', (name,))
        except sqlite3.IntegrityError:
          raise ReviewUnavailable('Connect returned duplicate inventory fields') from None
        reader.expect(':')
        reader.whitespace()
        if reader.peek() == '[':
          reader.expect('[')
          reader.whitespace()
          if reader.peek() != ']':
            while True:
              row = parse_url(reader.string(), identity)
              if row is not None:
                try:
                  rows.db.execute('INSERT INTO urls VALUES(?,?,?)', (row['number'], row['url'], row['filename']))
                except sqlite3.IntegrityError:
                  raise ReviewUnavailable('Connect returned duplicate original segment identities') from None
                rows.count += 1
                if rows.count % 256 == 0:
                  rows.db.commit()
              reader.whitespace()
              if reader.peek() == ']':
                break
              reader.expect(',')
          reader.expect(']')
        else:
          reader.scalar()
        reader.whitespace()
        if reader.peek() == '}':
          break
        reader.expect(',')
    reader.expect('}')
    reader.finish()
    if not rows.count:
      raise ReviewUnavailable('No original rlogs are available for this Connect route')
    if not permitted():
      raise InterruptedError('Connect inventory parsing paused')
    rows.db.commit()
    return rows
  except BaseException:
    rows.close()
    raise


def fetch_inventory(identity, store, *, permitted=lambda: True, get=None, api_factory=None, provider=None):
  """Device JWT is sent only to the fixed comma API, never to blob storage."""
  import requests
  from openpilot.common.api import Api
  from openpilot.common.version import get_version
  from openpilot.starpilot.connect.provider import active_provider

  device, _ = _identity(identity)
  get = requests.get if get is None else get
  api_factory = Api if api_factory is None else api_factory
  provider = active_provider if provider is None else provider
  if provider().name != 'comma':
    raise ReviewUnavailable('Select comma as the cloud provider to import Connect routes')
  if not permitted():
    raise InterruptedError('Connect inventory waits until parked')
  token = api_factory(device).get_token()
  if provider().name != 'comma' or not permitted():
    raise InterruptedError('Connect inventory provider or vehicle state changed')
  headers = {'Authorization': 'JWT ' + token, 'User-Agent': 'openpilot-' + get_version(), 'Accept-Encoding': 'identity'}
  try:
    with get('https://api.commadotai.com/v1/route/' + quote(identity, safe='') + '/files', headers=headers,
             timeout=(5, 20), stream=True, allow_redirects=False) as response:
      if response.status_code >= 500 or response.status_code in (408, 429):
        raise RetryableReview('Connect inventory is temporarily unavailable; completed progress is saved for retry')
      if response.status_code != 200:
        raise ReviewUnavailable('Connect route is unavailable or access was refused')
      return spool_inventory(identity, store, response.iter_content(CHUNK_BYTES),
                             permitted=lambda: permitted() and provider().name == 'comma')
  except InterruptedError:
    raise
  except requests.RequestException:
    raise RetryableReview('Connect inventory download interrupted; completed progress is saved for retry') from None
