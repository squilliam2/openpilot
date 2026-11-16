# Comma installer without a Wayland runtime

The hosted comma installer served for AGNOS 20.5.2 requires a Wayland EGL library
that is absent on the tested comma four. It exits before cloning. This bootstrap
is a static AArch64 executable with no interpreter or shared-library dependency.
It executes only the embedded fixed shell/Python transaction and leaves AGNOS
and graphics libraries unchanged.

Production accepts no arguments or alternate repository/path environment variables.
It requires an AGNOS AArch64 device, Python at `/usr/local/venv/bin/python3`, Git,
and 3 GiB free under `/data`. It refuses existing `openpilot` or `continue.sh`
targets, including dangling symlinks. A nonblocking lock prevents concurrent runs.
The private failure log is `/data/starpilot-installer.log`.

The transaction pins the published `squilliam2/openpilot:dom` commit, verifies the
clone and both existing source/prebuilt validators, then uses Linux atomic
`RENAME_NOREPLACE` for the install and continuation. `/data/continue.sh` is
published last. On failure it removes only its own staging directory; it never
sweeps other folders or replaces an existing install. It is for fresh setup.

The existing setup screen remains visible while the Git download runs. AGNOS
starts the installed software after the continuation appears. This bootstrap
does not draw a second installer screen.

Rebuild in Linux with Ubuntu's `gcc-aarch64-linux-gnu` and
`libc6-dev-arm64-cross` packages:

```sh
python3 tools/installer/build.py
python3 -m pytest -q -o addopts= tools/installer/tests/test_static_installer.py
```

`installer-dom.build.json` records compiler, flags, source/script hashes and the
binary hash. Identical inputs and compiler produce identical output; the build
adds no timestamp or build ID. Tests run transactions in private temporary
directories with injected Git/validation responses. They do not install on a
device or claim driving validation.
