"""All original sections compare directions; no declaration is actuator evidence."""

import hashlib

import pytest

from openpilot.starpilot.flm.route_tuning_consensus import RouteConsensus

VEHICLE = {'fingerprint': 'declared-car', 'vin': '0' * 17, 'deviceId': 'a' * 16, 'firmwareSha256': 'b' * 64}


def segment(number, *, proposals=None, config='c' * 64, digest=None):
  return {'segment': number, 'sourceSha256': digest or hashlib.sha256(str(number).encode()).hexdigest(),
          'vehicleIdentity': VEHICLE,
          'proposalGroups': [{'configurationHash': config, 'cohort': 'high:left:steady', 'windows': 12,
                              'proposals': [{'parameter': 'ff_gain_left', 'delta': .005}] if proposals is None else proposals}]}


def test_every_segment_after_fifty_and_after_512_contributes_without_cap(tmp_path):
  owner = RouteConsensus(tmp_path / 'all.sqlite', 'declared-route')
  for number in range(513):
    owner.add(segment(number))
  report = owner.summary()
  assert report['processedSegments'] == 513 and report['eligibleSegments'] == 513
  proposal = report['selected']
  assert proposal['training']['support'] == 257 and proposal['heldout']['support'] == 256
  assert proposal['parameter'] == 'ff_gain_left' and proposal['delta'] == .005
  assert not proposal['nativeQualified'] and not report['automaticApplicationAllowed']


def test_closed_store_resumes_exact_comparison_and_duplicates_do_not_vote(tmp_path):
  path = tmp_path / 'resume.sqlite'
  owner = RouteConsensus(path, 'declared-route')
  for number in range(6):
    assert owner.add(segment(number))
  initial = owner.summary()
  owner = RouteConsensus(path, 'declared-route')
  assert not owner.add(segment(0))
  assert not owner.add(segment(7, digest=segment(0)['sourceSha256']))
  later = owner.summary()
  assert later['duplicateSegments'] == 1 and later['eligibleSegments'] == 6
  assert later['selected']['training'] == initial['selected']['training']
  assert later['selected']['heldout'] == initial['selected']['heldout']
  with pytest.raises(ValueError, match='bytes changed'):
    owner.add(segment(0, digest='d' * 64))
  with pytest.raises(ValueError, match='another route'):
    RouteConsensus(path, 'other-route')


def test_no_change_sections_count_against_directional_agreement(tmp_path):
  owner = RouteConsensus(tmp_path / 'neutral.sqlite', 'declared-route')
  for number in range(20):
    owner.add(segment(number, proposals=[] if number >= 6 else None))
  assert owner.summary()['selected'] is None


def test_intra_section_conflict_cannot_create_multiple_votes(tmp_path):
  owner = RouteConsensus(tmp_path / 'conflict.sqlite', 'declared-route')
  for number in range(8):
    owner.add(segment(number, proposals=[{'parameter': 'ff_gain_left', 'delta': .005},
                                         {'parameter': 'ff_gain_left', 'delta': -.005}]))
  assert owner.summary()['selected'] is None


def test_heldout_rejects_one_training_choice_without_trying_the_next(tmp_path):
  owner = RouteConsensus(tmp_path / 'frozen.sqlite', 'declared-route')
  for number in range(20):
    proposals = [{'parameter': 'ff_gain_left', 'delta': .005 if number % 2 == 0 else -.005}]
    if number != 18:
      proposals.append({'parameter': 'ff_gain_right', 'delta': .005})
    owner.add(segment(number, proposals=proposals))
  report = owner.summary()
  assert report['selected'] is None
  assert report['comparison']['parameter'] == 'ff_gain_left'
  assert report['reason'] == 'heldout_sections_do_not_support_training_selected_direction'


def test_fast_policy_requires_stronger_empirical_support_and_creates_only_intent(tmp_path):
  owner = RouteConsensus(tmp_path / 'fast.sqlite', 'declared-route')
  for number in range(6):
    owner.add(segment(number))
  assert not owner.summary(convergence='faster')['selected']['fasterEvidenceSupported']
  for number in range(6, 30):
    owner.add(segment(number))
  report = owner.summary(convergence='faster')
  assert report['selected']['fasterEvidenceSupported']
  assert report['selected']['delta'] == .005 and not report['selected']['nativeQualified']
  assert report['requiresFreshNativeQualification']


def test_configuration_changes_are_never_pooled(tmp_path):
  owner = RouteConsensus(tmp_path / 'configs.sqlite', 'declared-route')
  for number in range(6):
    owner.add(segment(number, config=str(number) * 64))
  assert owner.summary()['selected'] is None


@pytest.mark.parametrize('mutate', [lambda row: row.update(segment=True),
                                   lambda row: row.update(sourceSha256='not-a-source'),
                                   lambda row: row['proposalGroups'][0]['proposals'][0].update(delta=float('nan')),
                                   lambda row: row['proposalGroups'][0]['proposals'][0].update(parameter='unknown')])
def test_invalid_source_or_suggestion_is_not_partially_committed(tmp_path, mutate):
  owner = RouteConsensus(tmp_path / 'invalid.sqlite', 'declared-route')
  row = segment(0)
  mutate(row)
  with pytest.raises(ValueError):
    owner.add(row)
  assert owner.summary()['processedSegments'] == 0
