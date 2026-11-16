#!/bin/bash
set -eu
exec /usr/local/venv/bin/python3 - <<'STARPILOT_INSTALLER_PYTHON'
@INSTALLER_PYTHON@
STARPILOT_INSTALLER_PYTHON
