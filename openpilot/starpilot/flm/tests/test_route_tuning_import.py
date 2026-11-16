"""Imports are source-bound intent; only native runtime reports trial outcomes."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openpilot.common.params import Params
from openpilot.starpilot.flm.canonical_surface_adapter import NAMES
from openpilot.starpilot.flm.offroad_store import RouteScope
from openpilot.starpilot.flm.route_tuning_import import enqueue, load_pending, mark_state, same_vehicle, snapshot, vehicle_identity


def declared(tmp_path):
  params = Params(str(tmp_path / 'params'))
  params.put('DongleId', 'a' * 16, block=True)
  cp = SimpleNamespace(carFingerprint='declared-car', carVin='0' * 17,
                       carFw=[SimpleNamespace(ecu='eps', address=1, subAddress=0, fwVersion=b'declared')])
  scope = RouteScope('a' * 64, 'declared-controller', 'b' * 64, 'c' * 64, '0', 'starpilot', 'torque')
  proposal = {'parameter': 'ff_gain_left', 'delta': .005, 'requiresFreshNativeQualification': True,
              'nativeQualified': False, 'convergence': 'faster', 'fasterEvidenceSupported': True,
              'sourceVehicle': vehicle_identity(cp, 'a' * 16)}
  previous = dict.fromkeys(NAMES, 0.)
  return params, cp, scope, proposal, previous


def load(params, cp, scope, previous, *, mode='live', current=None):
  with patch('openpilot.starpilot.flm.offroad_runtime.current_scope', return_value=scope if current is None else current), \
       patch('openpilot.starpilot.flm.learning_mode.selected_mode', return_value=mode):
    return load_pending(params, cp, scope, previous, mode)


def test_waiting_job_binds_once_and_never_writes_either_accepted_map(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  before = {key: params.get(key) for key in ('AutomaticSteeringProfiles', 'AutomaticSteeringLiveTrials')}
  identity = enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  assert snapshot(params)[0]['state'] == 'waiting_for_vehicle'
  candidate = load(params, cp, scope, previous)
  assert candidate.intent_id == identity and candidate.faster_evidence_supported
  hint = candidate.as_parked_candidate()
  assert hint.name == 'ff_gain_left' and hint.delta == .005
  assert hint.document()['requires_fresh_live_qualification']
  assert snapshot(params)[0]['state'] == 'queued'
  assert before == {key: params.get(key) for key in before}


def test_changed_profile_or_scope_cannot_rebase_same_historical_evidence(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  assert load(params, cp, scope, previous) is not None
  previous['ff_gain_left'] = .005
  assert load(params, cp, scope, previous) is None
  assert snapshot(params)[0]['state'] == 'stale_scope'
  previous['ff_gain_left'] = 0.
  assert load(params, cp, scope, previous) is None


def test_only_actual_native_lifecycle_can_finish_and_repeated_import_does_not_reapply(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  identity = enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  assert load(params, cp, scope, previous)
  with pytest.raises(ValueError, match='native trial'):
    mark_state(params, identity, 'confirmed')
  assert not mark_state(params, identity, 'confirmed', runtime_event=True)
  assert mark_state(params, identity, 'native_trial_offered', runtime_event=True)
  assert mark_state(params, identity, 'confirmed', runtime_event=True)
  assert enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal) == identity
  assert load(params, cp, scope, previous) is None
  assert snapshot(params)[0]['state'] == 'confirmed'


def test_wrong_device_or_missing_vehicle_identity_stays_unapplied(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  proposal['sourceVehicle']['deviceId'] = 'e' * 16
  enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  assert load(params, cp, scope, previous) is None
  assert snapshot(params)[0]['state'] == 'waiting_for_vehicle'
  assert params.get('AutomaticSteeringProfiles') is None and params.get('AutomaticSteeringLiveTrials') is None


def test_firmware_order_does_not_change_identity_and_known_vin_takes_precedence(tmp_path):
  _, cp, _, _, _ = declared(tmp_path)
  other = SimpleNamespace(ecu='camera', address=2, subAddress=0, fwVersion=b'camera')
  cp.carFw.append(other)
  original = vehicle_identity(cp, 'a' * 16)
  cp.carFw.reverse()
  assert original == vehicle_identity(cp, 'a' * 16)
  known = {**original, 'vin': 'JTMAB3FV0MD000001'}
  assert same_vehicle(known, {**known, 'deviceId': 'b' * 16})
  assert not same_vehicle(known, {**known, 'vin': 'JTMAB3FV0MD000002'})
  assert not same_vehicle(original, {**original, 'firmwareSha256': None})


def test_off_or_unreadable_current_configuration_supplies_no_import_permission(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  with patch('openpilot.starpilot.flm.offroad_runtime.current_scope', side_effect=ValueError('Automatic learning is off')), \
       patch('openpilot.starpilot.flm.learning_mode.selected_mode', return_value='live'):
    with pytest.raises(ValueError, match='off'):
      load_pending(params, cp, scope, previous, 'live')
  assert snapshot(params)[0]['state'] == 'waiting_for_vehicle'
  assert load(params, cp, scope, previous, current=replace(scope, reset_generation='1')) is None


def test_cancelled_enqueue_and_forged_qualified_proposal_are_refused(tmp_path):
  params, _, _, proposal, _ = declared(tmp_path)
  with pytest.raises(ValueError, match='interrupted'):
    enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal, authorized=lambda: False)
  proposal['nativeQualified'] = True
  with pytest.raises(ValueError, match='Unqualified'):
    enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)


def test_pid_controller_never_waits_for_an_unsupported_friction_trial(tmp_path):
  from openpilot.starpilot.flm.pid_stages import UNSUPPORTED
  params, cp, scope, proposal, previous = declared(tmp_path)
  scope = replace(scope, controller_domain='torque_pid')
  proposal['parameter'] = next(iter(UNSUPPORTED))
  enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  assert load(params, cp, scope, previous) is None
  assert snapshot(params)[0]['state'] == 'rejected'
  assert snapshot(params)[0]['reason'] == 'current_controller_does_not_support_this_coordinate'


def test_completed_jobs_do_not_fill_active_queue_or_allow_duplicate_route_reapplication(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  identities = []
  for number in range(40):
    identity = enqueue(params, route_id=f'declared-route-{number}', source_sha256=f'{number:064x}', proposal=proposal)
    identities.append(identity)
    assert load(params, cp, scope, previous).intent_id == identity
    assert mark_state(params, identity, 'native_trial_offered', runtime_event=True)
    assert mark_state(params, identity, 'confirmed', runtime_event=True)
  assert len(snapshot(params)) == 40
  assert all(row['state'] == 'confirmed' for row in snapshot(params))
  assert enqueue(params, route_id='declared-route-0', source_sha256=f'{0:064x}', proposal=proposal) == identities[0]
  assert load(params, cp, scope, previous) is None
  assert mark_state(params, identities[0], 'rolled_back', runtime_event=True, reason='later_native_tracking_regression')
  assert next(row for row in snapshot(params) if row['intentId'] == identities[0])['state'] == 'rolled_back'
  assert not mark_state(params, identities[0], 'queued')
  assert enqueue(params, route_id='declared-route-0', source_sha256=f'{0:064x}', proposal=proposal) == identities[0]
  assert load(params, cp, scope, previous) is None


def test_unfinished_job_capacity_never_discards_an_active_intent(tmp_path):
  params, _, _, proposal, _ = declared(tmp_path)
  for number in range(32):
    enqueue(params, route_id=f'declared-route-{number}', source_sha256=f'{number:064x}', proposal=proposal)
  with pytest.raises(ValueError, match='queue is full'):
    enqueue(params, route_id='declared-route-extra', source_sha256='e' * 64, proposal=proposal)
  assert len(snapshot(params)) == 32
