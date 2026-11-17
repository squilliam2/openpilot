"""Sparse sections pool original windows; no synthetic input is native authority."""

from dataclasses import replace
import hashlib

import pytest

from openpilot.starpilot.flm.canonical_surface_adapter import NAMES
from openpilot.starpilot.flm.offroad_evidence import RouteWindow, cohort, window_identity
from openpilot.starpilot.flm.route_tuning_consensus import RouteConsensus
from openpilot.starpilot.flm.route_window_transport import pack_window, unpack_window
from openpilot.starpilot.flm.tests.test_offroad_backend import TestOffroadTrackingPolicy as Fixtures


def section(number, gain=.85):
  original = Fixtures.windows('fixture', gain)[number % 3]
  points = tuple(replace(p, route='declared-route', segment=number, mono_ns=p.mono_ns+number*60_000_000_000,
                         control_header_ns=p.control_header_ns+number*60_000_000_000,
                         output_header_ns=p.output_header_ns+number*60_000_000_000) for p in original.points)
  window = RouteWindow(window_identity(points), points)
  return {'segment': number, 'sourceSha256': hashlib.sha256(str(number).encode()).hexdigest(),
          'vehicleIdentity': {'fingerprint': 'fixture'},
          'proposalGroups': [{'configurationHash': 'c'*64, 'cohort': cohort(window), 'windows': 1, 'proposals': [],
                              'previousCoordinates': dict.fromkeys(NAMES, 0.), 'trackingWindows': [pack_window(window)]}]}


def test_lossless_transport_retains_original_values_clocks_and_eligibility():
  packed = section(0)['proposalGroups'][0]['trackingWindows'][0]
  assert pack_window(unpack_window(packed)) == packed
  packed['points'][0][2] += .001
  with pytest.raises(ValueError, match='Unchanged'):
    unpack_window(packed)


def test_one_window_per_section_can_identify_a_route_wide_adjustment_and_resume(tmp_path):
  path = tmp_path/'pool.sqlite'
  owner = RouteConsensus(path, 'declared-route')
  for number in range(8):
    owner.add(section(number))
  result = RouteConsensus(path, 'declared-route').summary()
  selected = result['selected']
  assert result['pooledWindowCount'] == 8 and selected is not None
  assert selected['pooledComparison'] and selected['parameter'] == 'ff_gain_left'
  assert selected['training'] == {'sections': 4, 'windows': 4, 'fitWindows': 4, 'fitSections': 4}
  assert selected['heldout'] == {'sections': 4, 'windows': 4, 'fitWindows': 4, 'fitSections': 4}
  assert selected['delta'] == .005 and not selected['nativeQualified']
  assert selected['requiresFreshNativeQualification'] and not result['automaticApplicationAllowed']
  assert not owner.add(section(0))
  assert owner.summary() == result


def test_heldout_does_not_support_opposite_direction_or_allow_a_second_choice(tmp_path):
  owner = RouteConsensus(tmp_path/'opposite.sqlite', 'declared-route')
  for number in range(8):
    owner.add(section(number, gain=.85 if number % 2 == 0 else 1.15))
  result = owner.summary()
  assert result['selected'] is None and result['comparison']['parameter'] == 'ff_gain_left'
  assert result['reason'] == 'pooled_heldout_sections_do_not_support_training_selected_direction'


def test_duplicate_sections_and_wrong_original_id_cannot_inflate_pooled_support(tmp_path):
  owner = RouteConsensus(tmp_path/'copy.sqlite', 'declared-route')
  for number in range(4):
    owner.add(section(number))
  duplicate = section(4)
  duplicate['sourceSha256'] = section(0)['sourceSha256']
  owner.add(duplicate)
  assert owner.summary()['selected'] is None
  bad = section(5)
  bad['proposalGroups'][0]['trackingWindows'][0]['segment'] = 100
  with pytest.raises(ValueError):
    owner.add(bad)


def test_pooled_fit_honors_interruption_without_changing_saved_source(tmp_path):
  owner = RouteConsensus(tmp_path/'pause.sqlite', 'declared-route')
  for number in range(8):
    owner.add(section(number))
  with pytest.raises(InterruptedError, match='paused'):
    owner.summary(permitted=lambda: False)
  assert owner.summary()['selected'] is not None


def test_ignition_interrupting_the_estimator_is_retryable_and_keeps_checkpoint(tmp_path):
  owner = RouteConsensus(tmp_path/'mid-fit-pause.sqlite', 'declared-route')
  for number in range(8):
    owner.add(section(number))
  calls = [0]
  def permitted():
    calls[0] += 1
    return calls[0] < 8
  with pytest.raises(InterruptedError, match='paused'):
    owner.summary(permitted=permitted)
  assert owner.summary()['selected'] is not None
