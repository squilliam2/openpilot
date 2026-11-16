#!/usr/bin/env python3
"""Build the static ARM bootstrap reproducibly from explicit embedded sources."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def build(output, *, compiler='aarch64-linux-gnu-gcc'):
  here = Path(__file__).resolve().parent
  inputs = {name: (here / name).read_bytes() for name in ('bootstrap.c', 'bootstrap.sh', 'install.py')}
  template = inputs['bootstrap.sh'].decode()
  if template.count('@INSTALLER_PYTHON@') != 1:
    raise ValueError('Exactly one explicit embedded Python source is required')
  script = template.replace('@INSTALLER_PYTHON@', inputs['install.py'].decode()).encode()
  output = Path(output).resolve()
  output.parent.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(prefix='starpilot-bootstrap-build-') as folder:
    folder = Path(folder)
    (folder / 'bootstrap.c').write_bytes(inputs['bootstrap.c'])
    (folder / 'installer_script.h').write_text(
      'static const char installer_script[] = {' + ','.join(str(value) for value in script) + ',0};\n')
    flags = ['-std=c11', '-static', '-Os', '-s', '-fno-ident', '-Wall', '-Wextra', '-Werror', '-Wl,--build-id=none']
    subprocess.run([compiler, *flags, '-o', str(output), 'bootstrap.c'], cwd=folder, check=True,
                   env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'SOURCE_DATE_EPOCH': '0'})
  output.chmod(0o755)
  metadata = {'schemaVersion': 1, 'compiler': subprocess.check_output([compiler, '--version'], text=True).splitlines()[0],
              'flags': flags, 'sourceSha256': {name: hashlib.sha256(raw).hexdigest() for name, raw in inputs.items()},
              'embeddedScriptSha256': hashlib.sha256(script).hexdigest(),
              'elfSha256': hashlib.sha256(output.read_bytes()).hexdigest()}
  output.with_name(output.name + '.build.json').write_text(json.dumps(metadata, sort_keys=True, indent=2) + '\n')
  print(json.dumps(metadata, indent=2))
  return metadata


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[2] / 'installer-dom')
  args = parser.parse_args()
  build(args.output)
