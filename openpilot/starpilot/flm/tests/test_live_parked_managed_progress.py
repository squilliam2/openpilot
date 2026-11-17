"""The managed route owner supplies progress; parked overview never duplicates it."""

from unittest import mock

import pytest

from openpilot.starpilot.flm.live_parked_finish import LiveParkedFinisher
from openpilot.starpilot.flm.tests.test_power_loss_recovery import saved_vehicle
from openpilot.starpilot.galaxy.route_review_progress import managed_local_progress
from openpilot.starpilot.galaxy.tests.test_route_reviews_resume import owner, section


def setup(tmp_path, monkeypatch):
  params, _, _ = saved_vehicle(tmp_path)
  params.put('CurrentRoute', '00000001--aaaaaaaaaa', block=True)
  for number in range(3):
    section(tmp_path/'logs', number)
  reviews = owner(tmp_path, [], params=params)
  assert reviews.run_once()
  # Match the production storage resolution, without accessing real device state.
  read = managed_local_progress
  monkeypatch.setattr('openpilot.starpilot.galaxy.route_review_progress.managed_local_progress',
                      lambda params, root, **kwargs: read(params, root, store=tmp_path/'reviews', **kwargs))
  return params, reviews, LiveParkedFinisher(params, tmp_path/'logs', permitted=lambda: True)


def test_overview_observes_actual_partial_checkpoint_without_starting_another_fitter(tmp_path, monkeypatch):
  params, reviews, worker = setup(tmp_path, monkeypatch)
  before = {key: params.get(key) for key in ('AutomaticSteeringProfiles', 'AutomaticSteeringLiveTrials')}
  with mock.patch('openpilot.starpilot.flm.live_parked_finish.subprocess.Popen', side_effect=AssertionError('Duplicate fitter')):
    first = worker.run_once()
    assert first['managed_route_review'] and first['status'] == 'fitting'
    assert first['analysis_counts']['original_segments_reviewed'] == 1
    assert first['analysis_counts']['original_segments_available'] == 3
    assert reviews.run_once() and reviews.run_once()
    final = worker.run_once()
    assert final['finishing_completed'] and final['next_drive_candidate'] is None
    assert final['analysis_counts']['original_segments_reviewed'] == 3
    assert not final['actual_application_proven'] and not final['configuration_changed']
  assert before == {key: params.get(key) for key in before}


def test_source_repair_waits_for_reinventory_and_wrong_params_owner_cannot_delegate(tmp_path, monkeypatch):
  params, reviews, worker = setup(tmp_path, monkeypatch)
  section(tmp_path/'logs', 0).joinpath('rlog.zst').write_bytes(b'repaired original')
  result = worker.run_once()
  assert result['reason'] == 'waiting_for_resumable_local_route_inventory'
  assert not result['finishing_completed']
  other, _, _ = saved_vehicle(tmp_path/'other')
  assert managed_local_progress(other, tmp_path/'logs', store=tmp_path/'reviews') is None


def test_ignition_change_prevents_delegated_status_or_candidate_write(tmp_path, monkeypatch):
  params, reviews, worker = setup(tmp_path, monkeypatch)
  permissions = iter((True, False))
  worker.permitted = lambda: next(permissions, False)
  with mock.patch('openpilot.starpilot.flm.live_parked_finish.subprocess.Popen') as child:
    with pytest.raises(ValueError, match='interrupted'):
      worker.run_once()
  child.assert_not_called()


def test_late_galaxy_startup_reaps_an_already_started_duplicate_fitter(tmp_path):
  from openpilot.common.params import Params
  params = Params(str(tmp_path/'params'))
  worker = LiveParkedFinisher(params, tmp_path, permitted=lambda: True)
  managed = {'status': 'fitting', 'reason': 'resumable_local_route_analysis', 'finishing_completed': False}
  child = mock.Mock()
  child.poll.return_value = None
  with mock.patch.object(worker, '_managed_progress', side_effect=[None, managed]), \
       mock.patch('openpilot.starpilot.flm.live_parked_finish.subprocess.Popen', return_value=child), \
       mock.patch('openpilot.starpilot.flm.live_parked_finish.time.monotonic', side_effect=[0., 2.]):
    assert worker.run_once() == managed
  child.terminate.assert_called_once()
  child.wait.assert_called_once_with(timeout=2)


def test_registered_sonata_unknown_vin_can_observe_its_exact_managed_owner(tmp_path, monkeypatch):
  from opendbc.car.car_helpers import interfaces
  from opendbc.car.hyundai.values import CAR
  from openpilot.starpilot.schema_cache import put_cache
  params, reviews, worker = setup(tmp_path, monkeypatch)
  cp = interfaces[CAR.HYUNDAI_SONATA_HYBRID].get_non_essential_params(CAR.HYUNDAI_SONATA_HYBRID)
  cp.carVin = '0'*17
  cp.init('carFw', 1)[0].fwVersion = b'declared Sonata firmware'
  params.put('DongleId', 'a'*16, block=True)
  put_cache(params, 'CarParamsPersistent', cp, block=True)
  with mock.patch('openpilot.starpilot.flm.live_parked_finish.subprocess.Popen', side_effect=AssertionError('Duplicate fitter')):
    assert worker.run_once()['managed_route_review']
