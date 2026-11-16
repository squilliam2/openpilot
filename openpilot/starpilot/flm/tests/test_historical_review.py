"""Old recordings can yield measurements without inventing tuning authority."""
import pytest
import zstandard as zstd

from openpilot.cereal import log
from opendbc.car import structs
from openpilot.starpilot.flm.historical_review import review, selected_rows
from openpilot.starpilot.galaxy.drive_history import DriveHistory

ROUTE = '00000001--aaaaaaaaaa'


def recording(root, number=0, *, closed=True, speed=20., damaged=False, pid=False):
  folder = root / f'{ROUTE}--{number}'
  folder.mkdir(parents=True)
  cp = structs.CarParams.new_message(carFingerprint='DECLARED_HISTORICAL_CAR', carVin='0' * 17)
  messages = [log.Event.new_message(logMonoTime=1, valid=True, carParams=cp).to_bytes()]
  for index in range(301):
    stamp = 1_000_000_000 + number * 60_000_000_000 + index * 10_000_000
    event = log.Event.new_message(logMonoTime=stamp, valid=True)
    event.init('carState').vEgo = speed
    messages.append(event.to_bytes())
    event = log.Event.new_message(logMonoTime=stamp, valid=True)
    event.init('carControl').latActive = True
    messages.append(event.to_bytes())
    event = log.Event.new_message(logMonoTime=stamp, valid=True)
    state = event.init('controlsState').lateralControlState.init('pidState' if pid else 'torqueState')
    state.active = True
    if pid:
      state.steeringAngleDesiredDeg, state.steeringAngleDeg = 2., 1.75
    else:
      state.desiredLateralAccel, state.actualLateralAccel = 1., .75
    messages.append(event.to_bytes())
  if closed:
    event = log.Event.new_message(logMonoTime=stamp, valid=True)
    event.init('sentinel').type = 'endOfSegment'
    messages.append(event.to_bytes())
  raw = zstd.compress(b''.join(messages))
  (folder / 'rlog.zst').write_bytes(raw[:-3] if damaged else raw)
  return folder


def selected(root):
  return DriveHistory(root).snapshot()['routes'][0]


def test_unknown_vin_and_missing_route_scope_produce_measured_legacy_report(tmp_path):
  recording(tmp_path)
  result = review(tmp_path, selected(tmp_path))
  assert result['analysisOnly'] and not result['knownVinRecorded']
  assert not result['automaticApplicationAllowed'] and not result['tuneChanged']
  assert result['counts']['routeScopeFrames'] == 0
  assert result['reviewedSegments'] == [0]
  assert result['metrics'][0]['rmse'] == pytest.approx(.25)
  assert result['metrics'][0]['observedSeconds'] == 2.
  assert not list(tmp_path.glob('**/*profiles*'))


def test_pid_units_and_speed_cohorts_are_distinct(tmp_path):
  recording(tmp_path, speed=10., pid=True)
  recording(tmp_path, 1, speed=25., pid=True)
  result = review(tmp_path, selected(tmp_path))
  assert {m['speedRange'] for m in result['metrics']} == {'below_15_mps', 'at_least_15_mps'}
  assert all(m['domain'] == 'steering_angle' and m['rmse'] == pytest.approx(.25) for m in result['metrics'])


def test_damaged_and_unclosed_sources_are_omitted_without_counting_their_prefix(tmp_path):
  recording(tmp_path)
  recording(tmp_path, 1, damaged=True)
  recording(tmp_path, 2, closed=False)
  result = review(tmp_path, selected(tmp_path))
  assert result['reviewedSegments'] == [0]
  assert [row[0] for row in result['omittedSegments']] == [1, 2]
  assert not result['wholeRouteReviewed']
  assert result['metrics'][0]['samples'] == 201


def test_cancellation_and_foreign_cloud_device_cannot_publish_a_report(tmp_path, monkeypatch):
  from openpilot.starpilot.flm import historical_review as module
  recording(tmp_path)
  with pytest.raises(InterruptedError):
    review(tmp_path, selected(tmp_path), permitted=lambda: False)
  with pytest.raises(ValueError, match='requested device'):
    review(tmp_path, selected(tmp_path), expected_device='a' * 16)
  def cancelled(*args, **kwargs):
    raise module.LogDecodeCancelled('declared cancellation')
  monkeypatch.setattr(module, 'decode_full_rlog_segment', cancelled)
  with pytest.raises(InterruptedError):
    review(tmp_path, selected(tmp_path))


def test_long_route_selection_is_bounded_and_input_only():
  for count in (1, 14, 15, 180, 355, 512):
    rows = [{'number': n} for n in range(count)]
    chosen = selected_rows(rows)
    assert len(chosen) <= 14
    assert chosen[0] is rows[0] and chosen[-1] is rows[-1]
    assert len({r['number'] for r in chosen}) == len(chosen)


def test_actual_isolated_child_produces_a_bounded_observational_result(tmp_path):
  from openpilot.starpilot.galaxy.route_reviews import RouteReviews
  root = tmp_path / 'logs'
  recording(root)
  owner = RouteReviews(root, tmp_path / 'cache', permitted=lambda: True)
  try:
    result = owner._analyze(root, selected(root), permitted=lambda: True)
    assert result['analysisOnly'] and result['metrics'][0]['rmse'] == pytest.approx(.25)
    assert owner.child is None
  finally:
    owner.close()
