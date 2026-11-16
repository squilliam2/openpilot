"""Real parked resolution/CAS flow with declared source/native observations."""

from unittest.mock import patch

import pytest

from openpilot.starpilot.flm.learned_lifecycle import decode, encode
from openpilot.starpilot.flm.offroad_store import KEY, MAX_BYTES
from openpilot.starpilot.flm.tests.test_offroad_backend import scope
from openpilot.starpilot.flm.tests import test_offroad_coordinate_policy as fixtures
from openpilot.starpilot.flm.tests import test_offroad_retained_trials as retained_fixtures
from openpilot.starpilot.saved_source import read_saved


@pytest.mark.parametrize('conclusion,expected_event', [('accepted_after_drive', 'confirmed'),
                                                     ('retained_after_drive', 'confirmed'),
                                                     ('rolled_back', 'rolled_back')])
def test_actual_parked_outcome_updates_import_after_storage_resolves(tmp_path, conclusion, expected_event):
  fixture = fixtures.TestBoundedInconclusiveTrialLifecycle()
  params, cache, original, _ = fixture.saved_trial(str(tmp_path))
  imported_id = 'f' * 64
  original = dict(original, route_import_intent_id=imported_id)
  digest = cache.put(original)
  document = decode(read_saved(params, KEY, MAX_BYTES)[0])
  document['routeSurfaces'][scope().key()]['analysis_sha256'] = digest
  params.put(KEY, encode(document), block=True)

  observations = retained_fixtures.TestRetainedTrialSources()
  if conclusion == 'retained_after_drive':
    groups = observations.neutral_groups()
  else:
    _, observed = fixtures.TestComparableNextDriveOutcomes.documents()
    groups = observed.stable_groups
    if conclusion == 'rolled_back':
      groups[0]['cohorts'][0]['heldout']['error_reversal_hz'] = 2.

  events = []

  def recorded_event(actual_params, identity, state, *, runtime_event=False, reason=None):
    # The real compare-and-swap above must finish before the display changes.
    row = decode(read_saved(actual_params, KEY, MAX_BYTES)[0])['routeSurfaces'][scope().key()]
    assert row['state'] == conclusion
    assert identity == imported_id and runtime_event is True
    events.append((state, reason))
    return True

  with patch('openpilot.starpilot.flm.route_tuning_import.mark_state', side_effect=recorded_event):
    if conclusion == 'retained_after_drive':
      first = observations.drive(fixture, params, cache, str(tmp_path), '2', groups=groups)
      assert first['status'] == 'ready_for_next_drive' and not events
      result = observations.drive(fixture, params, cache, str(tmp_path), '3', groups=observations.neutral_groups())
    else:
      result = observations.drive(fixture, params, cache, str(tmp_path), '2', groups=groups)
  assert result['status'] == conclusion
  assert events == [(expected_event, result['reason'])]
