import bz2

from openpilot.cereal import log
from openpilot.starpilot.flm.full_route_review import analyze_segment
from openpilot.starpilot.flm.tests.test_power_loss_recovery import saved_vehicle, closed_section


def test_original_segment_analysis_keeps_clean_windows_and_records_no_application(tmp_path):
  _, cp, scope = saved_vehicle(tmp_path)
  path = closed_section(tmp_path / 'logs', cp, scope, 0)
  before = path.read_bytes()
  result = analyze_segment(path, 'bz2', number=0, route_id='00000001--aaaaaaaaaa')
  assert path.read_bytes() == before
  assert sum(metric['windows'] for metric in result['metrics']) > 0
  assert result['vehicleIdentity']['fingerprint'] == cp.carFingerprint
  assert result['proposalGroups']
  assert result['nativeQualified'] is False and result['actualApplicationProven'] is False


def test_manual_input_only_removes_affected_original_windows(tmp_path):
  _, cp, scope = saved_vehicle(tmp_path)
  path = closed_section(tmp_path / 'logs', cp, scope, 0)
  clean = analyze_segment(path, 'bz2', number=0, route_id='00000001--aaaaaaaaaa')
  changed = []
  for event in log.Event.read_multiple_bytes(bz2.decompress(path.read_bytes())):
    event = event.as_builder()
    if event.which() == 'carState' and event.logMonoTime == 31_000_000_000:
      event.carState.gasPressed = True
    changed.append(event.to_bytes())
  path.write_bytes(bz2.compress(b''.join(changed)))
  result = analyze_segment(path, 'bz2', number=0, route_id='00000001--aaaaaaaaaa')
  assert result['counts']['driver_input_intervals'] > 0
  assert 0 < sum(metric['windows'] for metric in result['metrics']) < sum(metric['windows'] for metric in clean['metrics'])


def test_actual_isolated_segment_worker_reviews_compressed_original_without_writes(tmp_path):
  from openpilot.starpilot.galaxy.stream_route_review import analyze_original
  params, cp, scope = saved_vehicle(tmp_path)
  path = closed_section(tmp_path / 'logs', cp, scope, 0)
  before = path.read_bytes()
  saved = {name: params.get(name) for name in ('AutomaticSteeringProfiles', 'AutomaticSteeringLiveTrials')}
  result = analyze_original(path, 'bz2', number=0, route_id='00000001--aaaaaaaaaa',
                            expected_device=None, permitted=lambda: True)
  assert result['proposalGroups'] and result['metrics']
  assert result['nativeQualified'] is False and result['actualApplicationProven'] is False
  assert path.read_bytes() == before
  assert {name: params.get(name) for name in saved} == saved
