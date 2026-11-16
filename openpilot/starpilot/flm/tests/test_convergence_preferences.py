"""Convergence is an optional parked vehicle choice, never tuning permission."""

import json
from unittest.mock import patch

import pytest

from openpilot.common.params import Params
from openpilot.starpilot.flm.tests.test_live_mode_selection import vehicle
from openpilot.starpilot.galaxy.flm_learning_mode import LearningMode
from openpilot.starpilot.lateral.learning_preferences import (
  KEY, convergence_mode, learning_mode, read_convergence_mode, read_preference,
  replace_convergence_mode, replace_learning_mode, replace_preference,
)
from openpilot.starpilot.schema_cache import put_cache


def test_convergence_defaults_to_standard_and_keeps_other_choices(tmp_path):
  params = Params(str(tmp_path / 'params'))
  cp = vehicle()
  assert convergence_mode(None, cp) == 'standard'
  raw = replace_learning_mode(replace_preference(None, cp, False), cp, 'parked')
  raw = replace_convergence_mode(raw, cp, 'faster')
  params.put(KEY, raw, block=True)
  assert learning_mode(raw, cp) == 'parked'
  assert not read_preference(params, cp).enabled
  assert read_convergence_mode(params, cp) == 'faster'
  assert convergence_mode(replace_preference(raw, cp, True), cp) == 'faster'
  other = cp.as_reader().as_builder()
  other.carFingerprint = 'ANOTHER_DECLARED_VEHICLE'
  assert convergence_mode(raw, other) == 'standard'
  second = replace_convergence_mode(raw, other, 'standard')
  assert convergence_mode(second, cp) == 'faster'
  assert convergence_mode(replace_convergence_mode(second, cp, 'standard'), cp) == 'standard'


@pytest.mark.parametrize('document', [
  {'version': 1, 'vehicles': {}, 'convergence': []},
  {'version': 1, 'vehicles': {}, 'convergence': {'toyota': []}},
  {'version': 1, 'vehicles': {}, 'convergence': {'toyota': {'declared': True}}},
  {'version': 1, 'vehicles': {}, 'convergence': {'toyota': {'declared': 'aggressive'}}},
  {'version': 1, 'vehicles': {}, 'convergence': {'': {'declared': 'faster'}}},
  {'version': 1, 'vehicles': {}, 'convergence': {'toyota': {'': 'faster'}}},
  {'version': 1, 'vehicles': {}, 'convergence': {'toyota': {str(i): 'faster' for i in range(65)}}},
  {'version': 1, 'vehicles': {}, 'unexpected': 'faster'},
])
def test_malformed_or_unknown_preferences_are_refused(document):
  with pytest.raises(ValueError):
    convergence_mode(json.dumps(document).encode(), vehicle())


def test_duplicate_json_keys_and_unsupported_vehicle_are_refused():
  cp = vehicle()
  with pytest.raises(ValueError, match='Duplicate'):
    convergence_mode(b'{"version":1,"vehicles":{},"convergence":{},"convergence":{}}', cp)
  raw = replace_convergence_mode(None, cp, 'faster')
  cp.passive = True
  assert convergence_mode(raw, cp) == 'standard'
  with pytest.raises(ValueError, match='supported'):
    replace_convergence_mode(raw, cp, 'faster')
  with pytest.raises(ValueError):
    replace_convergence_mode(raw, cp, 'anything')


def owner(tmp_path):
  params = Params(str(tmp_path / 'params'))
  cp = vehicle()
  params.put_bool('IsOffroad', True, block=True)
  params.put_bool('ForceAutoTuneOff', True, block=True)
  put_cache(params, 'CarParamsPersistent', cp, block=True)
  allowed = [True]
  return params, cp, allowed, LearningMode(params, parked=lambda: allowed[0])


def test_combined_save_is_one_cas_and_preserves_explicit_off(tmp_path):
  from openpilot.starpilot.galaxy import flm_learning_mode
  params, cp, allowed, selector = owner(tmp_path)
  original_cp = params.get('CarParamsPersistent')
  before = selector.snapshot()
  assert before['convergence'] == 'standard'
  with patch.object(flm_learning_mode, 'commit_exact', wraps=flm_learning_mode.commit_exact) as commit:
    after = selector.select('parked', before['revision'], convergence='faster', authorized=lambda: True)
  assert commit.call_count == 1
  assert (after['mode'], after['convergence'], after['enabled']) == ('parked', 'faster', False)
  assert params.get('CarParamsPersistent') == original_cp and params.get_bool('ForceAutoTuneOff')
  with pytest.raises(ValueError, match='changed'):
    selector.select_convergence('standard', before['revision'], authorized=lambda: True)
  allowed[0] = False
  with pytest.raises(ValueError, match='parked'):
    selector.select_convergence('standard', after['revision'], authorized=lambda: True)


def test_convergence_only_save_checks_session_and_vehicle_revision(tmp_path):
  params, cp, _, selector = owner(tmp_path)
  first = selector.snapshot()
  with pytest.raises(PermissionError):
    selector.select_convergence('faster', first['revision'], authorized=lambda: False)
  after = selector.select_convergence('faster', first['revision'], authorized=lambda: True)
  assert after['mode'] == 'live' and after['convergence'] == 'faster'
  cp.carFingerprint = 'ANOTHER_DECLARED_VEHICLE'
  put_cache(params, 'CarParamsPersistent', cp, block=True)
  with pytest.raises(ValueError, match='changed'):
    selector.select_convergence('standard', after['revision'], authorized=lambda: True)
  assert selector.snapshot()['convergence'] == 'standard'


def test_failed_authority_change_during_commit_cannot_change_preferences(tmp_path):
  params, _, _, selector = owner(tmp_path)
  first = selector.snapshot()
  calls = []
  def authorized():
    calls.append(None)
    return len(calls) < 2
  before = params.get(KEY)
  with pytest.raises(ValueError, match='could not be saved'):
    selector.select_convergence('faster', first['revision'], authorized=authorized)
  assert params.get(KEY) == before
