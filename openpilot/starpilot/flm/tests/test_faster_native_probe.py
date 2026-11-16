"""Numerical transport/amplitude tests do not establish road/native authority."""

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openpilot.starpilot.flm.canonical_surface_adapter import NAMES
from openpilot.starpilot.flm.live_tracking_probe import _physical_row, source_probe_step, validate_probe_result
from openpilot.starpilot.flm.live_tracking_probe_process import ProbeProcess, VERSION, _prepared_context, decode_probe_job, prepare_probe_payloads
from openpilot.starpilot.flm.live_tracking_response import SOURCE_VERSION
from openpilot.starpilot.flm.tests.test_live_tracking_probe import binding
from openpilot.starpilot.flm.tests.test_probe_batch_preparation import payloads


@pytest.mark.parametrize('step', [.01, .02])
def test_source_step_contract_accepts_only_existing_or_bounded_double(step):
  assert source_probe_step(step) == step


@pytest.mark.parametrize('step', [True, False, 0., -.01, .005, .03, .05, float('nan'), float('inf'), '.02'])
def test_unknown_or_excessive_source_probe_amplitude_is_refused_before_controller_work(step):
  with pytest.raises(ValueError, match='bounded doubled'):
    source_probe_step(step)


def test_prepared_child_hash_binds_real_requested_amplitude_and_legacy_default():
  rows = payloads()
  original = _prepared_context({'batch': rows})
  assert all(row['source_experiment_step'] == .01 for row in original['batch'])
  changed = deepcopy(rows)
  for row in changed:
    row['source_experiment_step'] = .02
  with pytest.raises(ValueError, match='original request'):
    prepare_probe_payloads(changed, queued_ns=11_000_000_000, expected=original)
  expected = _prepared_context({'batch': changed})
  prepared = prepare_probe_payloads(changed, queued_ns=11_000_000_000, expected=expected)
  import json
  envelope = json.loads(prepared['wire'])
  del envelope['preparedContextSha256']
  assert all(row['source_experiment_step'] == .02 for row in decode_probe_job(envelope)['batch'])


def decoded_fixture(requested, actual):
  # Literal numerical result transport, never an authentic source/control fixture.
  b = binding()
  accepted = tuple((name, 0.) for name in NAMES)
  data = {'binding': asdict(b), 'control': {'control_ns': 100, 'stage_schema_revision': 'torque-selected-stage-snapshot-v1'},
          'output': {'apply_ns': 110}, 'accepted_full30': accepted, 'group': 'ff.left', 'names': ('ff_gain_left',),
          'source_experiment_step': requested, 'witness': {'certificate': {'version': SOURCE_VERSION}}}
  row = _physical_row('ff_gain_left', 0., -actual, actual, .1, .09, .11, 1., 'separated')
  report = {'version': VERSION, 'sha256': 'a' * 64, 'rows': [asdict(row)]}
  return b, data, report


@pytest.mark.parametrize('requested,actual', [(.01, .02), (.02, .01), (.02, .05)])
def test_decoder_cannot_relabel_native_coverage_from_another_amplitude(requested, actual):
  b, data, report = decoded_fixture(requested, actual)
  with pytest.raises(ValueError, match='amplitude differs'):
    ProbeProcess.decode_report(report, expected=('a' * 64, data), prepared_sha=None, binding=b)


@pytest.mark.parametrize('amplitude', [.01, .02])
def test_exact_requested_native_coordinate_width_survives_transport(amplitude):
  b, data, report = decoded_fixture(amplitude, amplitude)
  result = ProbeProcess.decode_report(report, expected=('a' * 64, data), prepared_sha=None, binding=b)
  assert validate_probe_result(result, b) is result
  assert result.rows[0].minus_coordinate == -amplitude and result.rows[0].plus_coordinate == amplitude


@pytest.mark.parametrize('widths,expected', [([.01] * 4, .01), ([.02] * 4, .02), ([.01, .02, .02, .02], 0.)])
def test_episode_keeps_common_actual_width_and_mixed_amplitudes_prove_neither(widths, expected):
  from openpilot.starpilot.flm.live_tracking_episodes import TrackingEpisodeBuilder
  points = [SimpleNamespace(error=.1) for _ in widths]
  rows = {'ff_gain_left': [(point, _physical_row('ff_gain_left', 0., -width, width, .1, .09, .11, 1., 'separated'))
                           for point, width in zip(points, widths, strict=True)]}
  builder = object.__new__(TrackingEpisodeBuilder)
  with patch.object(TrackingEpisodeBuilder, '_gradient_rows', return_value=rows), \
       patch.object(TrackingEpisodeBuilder, '_gradient_coverage', return_value={'ff_gain_left': True}):
    feature, = builder._sensitivities(points, {})
  assert feature.minus_probed_step == expected and feature.plus_probed_step == expected
  feature.validate()
