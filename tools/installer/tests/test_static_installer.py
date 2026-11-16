"""Transaction tests use private temporary directories, never /data or a device."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('static_dom_installer', HERE / 'install.py')
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
SHA = 'a' * 40


class Source:
  def __init__(self, root, failure=None):
    self.root, self.failure, self.calls = root, failure, []

  def __call__(self, args, *, log, timeout=90, cwd=None, capture=False):
    self.calls.append((args, cwd))
    assert not (self.root / 'continue.sh').exists()
    assert not (self.root / 'openpilot').exists()
    if 'ls-remote' in args:
      return SHA + '\trefs/heads/dom\n'
    if 'clone' in args:
      if self.failure == 'clone':
        raise subprocess.CalledProcessError(128, args)
      source = Path(args[-1])
      source.mkdir()
      for name in ('launch_openpilot.sh', 'launch_chffrplus.sh'):
        (source / name).write_text('#!/bin/bash\nexit 0\n')
        (source / name).chmod(0o755)
    elif 'rev-parse' in args:
      return ('b' * 40 if self.failure == 'changed_commit' else SHA) + '\n'
    elif 'tools/vendor/check.py' in args and self.failure == 'vendor':
      raise subprocess.CalledProcessError(1, args)
    elif 'tools/ci/check_prebuilt_receipt.py' in args and self.failure == 'receipt':
      raise subprocess.CalledProcessError(1, args)
    elif 'diff' in args and self.failure == 'modified':
      raise subprocess.CalledProcessError(1, args)
    elif 'ls-files' in args:
      return 'unexpected.py\n' if self.failure == 'untracked' else ''
    return None


def run(root, **kwargs):
  return installer.install(root, minimum_free=0, **kwargs)


def test_success_pins_public_source_checks_receipts_and_publishes_continue_last(tmp_path):
  source = Source(tmp_path)
  assert run(tmp_path, run=source) == SHA
  assert (tmp_path / 'openpilot/launch_openpilot.sh').is_file()
  assert (tmp_path / 'continue.sh').read_text() == (
    f'#!/usr/bin/env bash\nset -e\ncd {tmp_path}/openpilot\nexec ./launch_openpilot.sh\n')
  assert (tmp_path / 'continue.sh').stat().st_mode & 0o777 == 0o755
  assert (tmp_path / 'starpilot-installer.log').stat().st_mode & 0o777 == 0o600
  assert not list(tmp_path.glob('.starpilot-install-*'))
  clone = next(args for args, _ in source.calls if 'clone' in args)
  assert installer.REPOSITORY in clone and '--no-recurse-submodules' in clone and '--depth=1' in clone
  assert any('tools/vendor/check.py' in args for args, _ in source.calls)
  receipt = next(args for args, _ in source.calls if 'tools/ci/check_prebuilt_receipt.py' in args)
  assert receipt[-1] == SHA


@pytest.mark.parametrize('failure', ['clone', 'changed_commit', 'vendor', 'receipt', 'modified', 'untracked'])
def test_failed_clone_or_validation_cannot_install_or_sweep_other_staging(tmp_path, failure):
  other = tmp_path / '.starpilot-install-someone-else'
  other.mkdir()
  (other / 'keep').write_text('not ours')
  with pytest.raises((subprocess.CalledProcessError, RuntimeError)):
    run(tmp_path, run=Source(tmp_path, failure))
  assert not os.path.lexists(tmp_path / 'openpilot') and not os.path.lexists(tmp_path / 'continue.sh')
  assert list(tmp_path.glob('.starpilot-install-*')) == [other]
  assert (other / 'keep').read_text() == 'not ours'
  assert 'Installation stopped:' in (tmp_path / 'starpilot-installer.log').read_text()


@pytest.mark.parametrize('name', ['openpilot', 'continue.sh'])
@pytest.mark.parametrize('kind', ['file', 'directory', 'dangling_link'])
def test_existing_installation_or_link_is_never_replaced(tmp_path, name, kind):
  path = tmp_path / name
  if kind == 'directory':
    path.mkdir()
  elif kind == 'dangling_link':
    path.symlink_to(tmp_path / 'absent')
  else:
    path.write_text('existing')
  original = path.lstat()
  source = Source(tmp_path)
  with pytest.raises(RuntimeError, match='Refusing to replace'):
    run(tmp_path, run=source)
  assert path.lstat().st_ino == original.st_ino and source.calls == []


@pytest.mark.parametrize('name', ['starpilot-installer.log', '.starpilot-installer.lock'])
def test_log_and_lock_symlinks_do_not_truncate_other_files(tmp_path, name):
  other = tmp_path / 'unrelated'
  other.write_text('keep')
  (tmp_path / name).symlink_to(other)
  with pytest.raises(OSError):
    run(tmp_path, run=Source(tmp_path))
  assert other.read_text() == 'keep'


def test_hardlinked_log_is_refused_before_truncating(tmp_path):
  other = tmp_path / 'unrelated'
  other.write_text('keep')
  os.link(other, tmp_path / 'starpilot-installer.log')
  with pytest.raises(RuntimeError, match='Unsafe installer'):
    run(tmp_path, run=Source(tmp_path))
  assert other.read_text() == 'keep'


def test_concurrent_installation_is_refused_without_changing_log(tmp_path):
  path = tmp_path / '.starpilot-installer.lock'
  with path.open('wb') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (tmp_path / 'starpilot-installer.log').write_text('other installation')
    with pytest.raises(RuntimeError, match='already running'):
      run(tmp_path, run=Source(tmp_path))
    assert (tmp_path / 'starpilot-installer.log').read_text() == 'other installation'


def test_continuation_race_is_not_clobbered_and_our_tree_rolls_back(tmp_path):
  rename = installer._rename_new
  def race(source, destination):
    if destination == tmp_path / 'continue.sh':
      destination.write_text('another installer')
    rename(source, destination)
  with patch.object(installer, '_rename_new', side_effect=race):
    with pytest.raises(FileExistsError):
      run(tmp_path, run=Source(tmp_path))
  assert (tmp_path / 'continue.sh').read_text() == 'another installer'
  assert not (tmp_path / 'openpilot').exists() and not list(tmp_path.glob('.starpilot-install-*'))


def test_install_destination_race_never_replaces_other_tree(tmp_path):
  rename = installer._rename_new
  def race(source, destination):
    if destination == tmp_path / 'openpilot':
      destination.mkdir()
      (destination / 'keep').write_text('someone else')
    rename(source, destination)
  with patch.object(installer, '_rename_new', side_effect=race):
    with pytest.raises(FileExistsError):
      run(tmp_path, run=Source(tmp_path))
  assert (tmp_path / 'openpilot/keep').read_text() == 'someone else'
  assert not (tmp_path / 'continue.sh').exists()


def test_production_main_refuses_arguments_before_device_or_install_work():
  with patch.object(installer.sys, 'argv', ['installer', '/some/other/path']):
    with pytest.raises(RuntimeError, match='no arguments'):
      installer.main()


def test_root_must_not_be_a_symlink(tmp_path):
  linked = tmp_path / 'link'
  linked.symlink_to(tmp_path, target_is_directory=True)
  with pytest.raises(RuntimeError, match='real installation'):
    run(linked, run=Source(tmp_path))


def test_timeout_stops_git_helpers_in_its_private_process_group(tmp_path):
  pidfile = tmp_path / 'helper.pid'
  script = ('import pathlib,subprocess,time; child=subprocess.Popen(["sleep","60"]); ' +
            f'pathlib.Path({str(pidfile)!r}).write_text(str(child.pid)); time.sleep(60)')
  with (tmp_path / 'output').open('w') as log:
    with pytest.raises(subprocess.TimeoutExpired):
      installer._run([sys.executable, '-c', script], log=log, timeout=.2)
  pid = int(pidfile.read_text())
  state = Path(f'/proc/{pid}/stat')
  assert not state.exists() or state.read_text().split()[2] == 'Z'


def test_compiled_launcher_refuses_arguments_before_executing_embedded_script(tmp_path):
  # Host compilation exercises C argument handling without executing an ARM ELF.
  (tmp_path / 'installer_script.h').write_text('static const char installer_script[] = "exit 99";\n')
  binary = tmp_path / 'argument-check'
  subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(tmp_path),
                  str(HERE / 'bootstrap.c'), '-o', str(binary)], check=True)
  result = subprocess.run([str(binary), '--another-repository'], capture_output=True, text=True)
  assert result.returncode == 64 and 'no arguments' in result.stderr


def test_published_binary_is_static_arm_and_matches_build_receipt():
  import hashlib
  binary = HERE.parents[1] / 'installer-dom'
  receipt = json.loads(binary.with_name('installer-dom.build.json').read_text())
  raw = binary.read_bytes()
  assert raw[:4] == b'\x7fELF' and raw[4:6] == bytes((2, 1))
  assert int.from_bytes(raw[18:20], 'little') == 183
  assert hashlib.sha256(raw).hexdigest() == receipt['elfSha256']
  for name, digest in receipt['sourceSha256'].items():
    assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == digest
  headers = subprocess.check_output(['readelf', '-l', str(binary)], text=True)
  dynamic = subprocess.check_output(['readelf', '-d', str(binary)], text=True)
  assert 'INTERP' not in headers and '(NEEDED)' not in dynamic
