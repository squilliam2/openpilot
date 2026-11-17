"""Imports are source-bound intent; only native runtime reports trial outcomes."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openpilot.common.params import Params
from openpilot.starpilot.flm.canonical_surface_adapter import NAMES
from openpilot.starpilot.flm.offroad_store import RouteScope
from openpilot.starpilot.flm.route_tuning_import import enqueue, load_pending, mark_state, retire_unoffered, same_vehicle, snapshot, vehicle_identity


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


def test_changed_route_proof_retires_older_unoffered_direction_instead_of_stacking_it(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  original = enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  assert load(params, cp, scope, previous).intent_id == original
  updated = enqueue(params, route_id='declared-route', source_sha256='e' * 64, proposal=proposal)
  assert updated != original
  states = {row['intentId']: row['state'] for row in snapshot(params)}
  assert states == {original: 'stale_scope', updated: 'waiting_for_vehicle'}
  assert load(params, cp, scope, previous).intent_id == updated
  assert mark_state(params, updated, 'native_trial_offered', runtime_event=True)
  assert mark_state(params, updated, 'confirmed', runtime_event=True)
  previous['ff_gain_left'] = .01
  assert load(params, cp, scope, previous) is None


def test_source_invalidation_with_no_new_consensus_disables_only_unoffered_intents_for_that_route(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  offered = enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  assert load(params, cp, scope, previous).intent_id == offered
  assert mark_state(params, offered, 'native_trial_offered', runtime_event=True)
  pending = enqueue(params, route_id='declared-route', source_sha256='e' * 64, proposal=proposal)
  other = enqueue(params, route_id='another-route', source_sha256='f' * 64, proposal=proposal)
  assert retire_unoffered(params, route_id='declared-route') == 1
  states = {row['intentId']: row['state'] for row in snapshot(params)}
  assert states == {offered: 'native_trial_offered', pending: 'stale_scope', other: 'waiting_for_vehicle'}
  assert retire_unoffered(params, route_id='declared-route') == 0
  assert mark_state(params, offered, 'confirmed', runtime_event=True)
  assert params.get('AutomaticSteeringProfiles') is None and params.get('AutomaticSteeringLiveTrials') is None


def test_identical_revalidated_bytes_resume_only_original_binding_and_do_not_duplicate_the_intent(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  identity = enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  first = load(params, cp, scope, previous)
  assert retire_unoffered(params, route_id='declared-route', source_sha256='d' * 64) == 0
  assert retire_unoffered(params, route_id='declared-route') == 1
  assert load(params, cp, scope, previous) is None
  assert enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal) == identity
  resumed = load(params, cp, scope, previous)
  assert resumed.intent_id == identity and resumed.previous == first.previous and resumed.scope == first.scope
  assert enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal) == identity
  assert len(snapshot(params)) == 1
  assert retire_unoffered(params, route_id='declared-route') == 1
  previous['ff_gain_left'] = .01
  assert enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal) == identity
  assert load(params, cp, scope, previous) is None
  assert snapshot(params)[0]['reason'] == 'vehicle_settings_reset_or_accepted_profile_changed'
  assert enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal) == identity
  previous['ff_gain_left'] = 0.
  assert load(params, cp, scope, previous) is None


def test_source_revalidation_survives_queue_archival_but_cannot_resurrect_a_native_success(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  identity = enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  first = load(params, cp, scope, previous)
  assert retire_unoffered(params, route_id='declared-route') == 1
  others = []
  for number in range(32):
    others.append(enqueue(params, route_id=f'other-route-{number}', source_sha256=f'{number:064x}', proposal=proposal))
  assert next(row for row in snapshot(params) if row['intentId'] == identity)['state'] == 'stale_scope'
  assert mark_state(params, others[-1], 'rejected', reason='declared_unsupported_controller')
  assert enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal) == identity
  resumed = load(params, cp, scope, previous)
  assert resumed.intent_id == identity and resumed.previous == first.previous and resumed.scope == first.scope
  assert mark_state(params, identity, 'native_trial_offered', runtime_event=True)
  assert mark_state(params, identity, 'confirmed', runtime_event=True)
  enqueue(params, route_id='last-route', source_sha256='a' * 64, proposal=proposal)
  assert enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal) == identity
  assert next(row for row in snapshot(params) if row['intentId'] == identity)['state'] == 'confirmed'


def test_cancelled_source_reconciliation_cannot_change_queue_or_either_accepted_map(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  identity = enqueue(params, route_id='declared-route', source_sha256='d' * 64, proposal=proposal)
  assert load(params, cp, scope, previous).intent_id == identity
  before = snapshot(params)
  accepted = {key: params.get(key) for key in ('AutomaticSteeringProfiles', 'AutomaticSteeringLiveTrials')}
  permission = iter((True, False))
  with pytest.raises(ValueError, match='interrupted'):
    retire_unoffered(params, route_id='declared-route', authorized=lambda: next(permission))
  assert snapshot(params) == before
  assert {key: params.get(key) for key in accepted} == accepted


@pytest.mark.parametrize('cloud_sha', ['d' * 64, 'e' * 64])
def test_own_connect_and_local_aliases_leave_one_unoffered_intent_without_merging_foreign_devices(tmp_path, cloud_sha):
  params, _, _, proposal, _ = declared(tmp_path)
  route = '00000001--aaaaaaaaaa'
  local = enqueue(params, route_id=route, source_sha256='d' * 64, proposal=proposal)
  cloud = enqueue(params, route_id='a' * 16 + '|' + route, source_sha256=cloud_sha, proposal=proposal)
  foreign = enqueue(params, route_id='b' * 16 + '|' + route, source_sha256='f' * 64, proposal=proposal)
  states = {row['intentId']: row['state'] for row in snapshot(params)}
  assert states == {local: 'stale_scope', cloud: 'waiting_for_vehicle', foreign: 'waiting_for_vehicle'}
  assert retire_unoffered(params, route_id=route, source_sha256=cloud_sha) == 0
  assert retire_unoffered(params, route_id=route) == 1
  assert next(row for row in snapshot(params) if row['intentId'] == foreign)['state'] == 'waiting_for_vehicle'


def test_cross_source_retirement_preserves_offered_native_trial_and_unknown_device_is_not_an_alias(tmp_path):
  params, cp, scope, proposal, previous = declared(tmp_path)
  route = '00000001--aaaaaaaaaa'
  offered = enqueue(params, route_id=route, source_sha256='d' * 64, proposal=proposal)
  assert load(params, cp, scope, previous).intent_id == offered
  assert mark_state(params, offered, 'native_trial_offered', runtime_event=True)
  pending = enqueue(params, route_id='a' * 16 + '|' + route, source_sha256='e' * 64, proposal=proposal)
  assert retire_unoffered(params, route_id=route) == 1
  assert next(row for row in snapshot(params) if row['intentId'] == offered)['state'] == 'native_trial_offered'
  assert mark_state(params, offered, 'confirmed', runtime_event=True)
  assert next(row for row in snapshot(params) if row['intentId'] == pending)['state'] == 'stale_scope'
  params.put('DongleId', 'unknown', block=True)
  another = '00000002--bbbbbbbbbb'
  first = enqueue(params, route_id=another, source_sha256='d' * 64, proposal=proposal)
  second = enqueue(params, route_id='a' * 16 + '|' + another, source_sha256='e' * 64, proposal=proposal)
  states = {row['intentId']: row['state'] for row in snapshot(params)}
  assert states[first] == states[second] == 'waiting_for_vehicle'
