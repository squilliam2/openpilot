"""Fixed fresh-install transaction, embedded in installer-dom; no OS changes."""

from contextlib import contextmanager
import ctypes
import fcntl
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

REPOSITORY = 'https://github.com/squilliam2/openpilot.git'
BRANCH = 'dom'
DATA_ROOT = Path('/data')
GIT = ['git', '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null',
       '-c', 'protocol.file.allow=never', '-c', 'submodule.recurse=false']


def _absent(path):
  if os.path.lexists(path):
    raise RuntimeError(f'Refusing to replace existing {path.name}')


def _identity(path):
  info = path.lstat()
  return info.st_dev, info.st_ino


def _same_identity(path, identity):
  try:
    return _identity(path) == identity
  except OSError:
    return False


def _rename_new(source, destination):
  """Linux atomic rename with NOREPLACE, including symlinks and empty folders."""
  libc = ctypes.CDLL(None, use_errno=True)
  rename = libc.renameat2
  rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
  rename.restype = ctypes.c_int
  if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error), str(destination))


def _sync_directory(path):
  fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
  try:
    os.fsync(fd)
  finally:
    os.close(fd)


@contextmanager
def _owned_file(path, *, truncate=False):
  fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
      raise RuntimeError(f'Unsafe installer file: {path.name}')
    if truncate:
      os.ftruncate(fd, 0)
    yield fd
  finally:
    os.close(fd)


def _run(args, *, log, timeout=90, cwd=None, capture=False):
  print('Running: ' + shlex.join(args), file=log, flush=True)
  child = subprocess.Popen(args, cwd=cwd, start_new_session=True,
                           stdout=subprocess.PIPE if capture else log, stderr=log, text=capture)
  try:
    output, _ = child.communicate(timeout=timeout)
    if child.returncode:
      raise subprocess.CalledProcessError(child.returncode, args, output=output)
    return output if capture else None
  except BaseException:
    # Git can spawn helpers. Stop the entire private session on timeout/signal.
    try:
      os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
      pass
    try:
      child.communicate(timeout=2)
    except subprocess.TimeoutExpired:
      try:
        os.killpg(child.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      child.communicate(timeout=2)
    raise


def install(root, *, run=_run, minimum_free=3 * 1024**3):
  """Only main() chooses production paths; tests inject a separate temporary root."""
  root = Path(root)
  if not root.is_absolute() or not stat.S_ISDIR(root.lstat().st_mode) or root.resolve() != root:
    raise RuntimeError('A real installation data directory is required')
  destination, continuation = root / 'openpilot', root / 'continue.sh'
  stage = stage_identity = installed_identity = None
  committed = False
  with _owned_file(root / '.starpilot-installer.lock') as lock:
    try:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      raise RuntimeError('Another StarPilot installation is already running') from None
    with _owned_file(root / 'starpilot-installer.log', truncate=True) as log_fd:
      with os.fdopen(os.dup(log_fd), 'w', buffering=1) as log:
        try:
          print('Fresh StarPilot Dom installation; AGNOS remains unchanged.', file=log)
          _absent(destination)
          _absent(continuation)
          if shutil.disk_usage(root).free < minimum_free:
            raise RuntimeError('At least 3 GiB free space is required')
          selected = run([*GIT, 'ls-remote', '--exit-code', REPOSITORY, f'refs/heads/{BRANCH}'],
                         log=log, timeout=60, capture=True)
          match = re.fullmatch(r'([0-9a-f]{40})\s+refs/heads/dom\n?', selected or '')
          if match is None:
            raise RuntimeError('The published Dom commit is unavailable or ambiguous')
          commit = match[1]
          print(f'Pinned source commit: {commit}', file=log)
          stage = Path(tempfile.mkdtemp(prefix='.starpilot-install-', dir=root))
          stage_identity = _identity(stage)
          source = stage / 'source'
          run([*GIT, 'clone', '--progress', '--depth=1', '--single-branch', '--no-recurse-submodules',
               '--branch', BRANCH, REPOSITORY, str(source)], log=log, timeout=600)
          actual = run([*GIT, '-C', str(source), 'rev-parse', '--verify', 'HEAD^{commit}'],
                       log=log, capture=True)
          if actual.strip() != commit:
            raise RuntimeError('Dom changed during download; retry to pin the new revision')
          run([sys.executable, 'tools/vendor/check.py'], cwd=source, log=log)
          run([sys.executable, 'tools/ci/check_prebuilt_receipt.py', '--repo', '.', '--revision', commit],
              cwd=source, log=log)
          run([*GIT, '-C', str(source), 'diff', '--exit-code', 'HEAD', '--'], log=log)
          extra = run([*GIT, '-C', str(source), 'ls-files', '--others', '--exclude-standard'], log=log, capture=True)
          if extra:
            raise RuntimeError('Unexpected files appeared in the source checkout')
          for name in ('launch_openpilot.sh', 'launch_chffrplus.sh'):
            info = (source / name).lstat()
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_IXUSR:
              raise RuntimeError(f'Invalid executable entry point: {name}')
          run(['sync', '-f', str(source)], log=log, timeout=60)
          pending = stage / 'continue.sh'
          with pending.open('xb') as file:
            file.write(('#!/usr/bin/env bash\nset -e\ncd ' + shlex.quote(str(destination)) +
                        '\nexec ./launch_openpilot.sh\n').encode())
            os.fchmod(file.fileno(), 0o755)
            file.flush()
            os.fsync(file.fileno())
          _absent(destination)
          _absent(continuation)
          if _identity(stage) != stage_identity:
            raise RuntimeError('Installer staging directory changed')
          _rename_new(source, destination)
          installed_identity = _identity(destination)
          _sync_directory(root)
          # AGNOS starts the software as soon as this marker appears. It is last.
          _rename_new(pending, continuation)
          committed = True
          _sync_directory(root)
          print(f'Installed {commit}; handing startup to AGNOS.', file=log)
          os.fsync(log.fileno())
          return commit
        except BaseException as error:
          print(f'Installation stopped: {type(error).__name__}: {error}', file=log)
          os.fsync(log.fileno())
          raise
        finally:
          # Restore only our own renamed tree if the continuation was not published.
          if stage is not None and installed_identity is not None and not committed and _same_identity(destination, installed_identity):
            _rename_new(destination, stage / 'source')
            _sync_directory(root)
          # Never sweep a prefix or delete someone else's staging directory.
          if stage is not None and _same_identity(stage, stage_identity):
            shutil.rmtree(stage)


def main():
  if len(sys.argv) != 1:
    raise RuntimeError('This installer accepts no arguments')
  if not Path('/AGNOS').is_file() or platform.machine() != 'aarch64':
    raise RuntimeError('This installer requires an AGNOS comma device')
  def stopped(signum, frame):
    raise RuntimeError(f'Installer interrupted by signal {signum}')
  for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
    signal.signal(signum, stopped)
  signal.alarm(900)
  try:
    install(DATA_ROOT)
  finally:
    signal.alarm(0)


if __name__ == '__main__':
  main()
