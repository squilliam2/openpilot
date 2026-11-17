"""Transport-only fixtures; numerical/native admission stays in the worker."""
from copy import deepcopy
import hashlib
import json
from unittest import TestCase
from unittest.mock import patch

import pytest

from openpilot.starpilot.flm.controller_replay import pack_primitive_snapshot
from openpilot.starpilot.flm.live_tracking_probe import _primitive
from openpilot.starpilot.flm.live_tracking_probe_process import (
  NUMERICAL_DEADLINE_NS, ProbeProcess, VERSION, _prepared_context, decode_probe_job, prepare_probe_payloads, prepare_probe_wire,
)


def payloads():
  rows = []
  for offset in range(4):
    stamp = 10_000_000_000 + offset * 10_000_000
    control = {'control_ns': stamp, 'stage_schema_revision': 'transport-only', 'replay_snapshot': (('x', 0.),)}
    rows.append({'version': VERSION, 'replay': b'transport-only', 'prefix_controls': (control,),
                 'control': control, 'output': {'apply_ns': stamp + 1}, 'binding': {'identity': 'transport-only'},
                 'accepted_full30': (), 'anchor': (1., 1., 1.), 'group': 'all30', 'names': ('transport-only',),
                 'archived_clock': False, 'source': {'enabled': False}, 'evidence_ns': 11_000_000_000,
                 'queued_ns': 11_000_000_000})
  return tuple(rows)


def legacy(rows):
  jobs = []
  for row in rows:
    packed = pack_primitive_snapshot(row)
    jobs.append({'version': VERSION, 'payload': packed.hex(), 'sha256': hashlib.sha256(packed).hexdigest()})
  return jobs


class ProbeBatchPreparationTest(TestCase):
  def test_direct_preparation_preserves_every_decoded_field_and_context(self):
    rows = payloads()
    context = _prepared_context({'batch': rows})
    old = prepare_probe_wire(legacy(rows), queued_ns=11_000_000_000, expected=context)
    new = prepare_probe_payloads(rows, queued_ns=11_000_000_000, expected=context)
    def decoded(result):
      wire = json.loads(result['wire'])
      del wire['preparedContextSha256']
      return _primitive(decode_probe_job(wire))
    self.assertEqual(decoded(old), decoded(new))
    self.assertEqual(old['context_sha256'], new['context_sha256'])

  def test_direct_preparation_keeps_original_order_and_two_second_input_bound(self):
    rows = payloads()
    context = _prepared_context({'batch': rows})
    for changed, clock in (((), 11_000_000_000), (rows + rows[:1], 11_000_000_000),
                            (tuple(reversed(rows)), 11_000_000_000), (rows, 13_000_000_001)):
      with self.subTest(clock=clock, count=len(changed)), self.assertRaises(ValueError):
        prepare_probe_payloads(changed, queued_ns=clock, expected=context)

  def test_direct_preparation_rejects_changed_or_nonprimitive_inputs(self):
    rows = payloads()
    context = _prepared_context({'batch': rows})
    for field, value in (('version', 'unknown'), ('replay', b''), ('prefix_controls', ()),
                         ('anchor', (float('nan'),)), ('queued_ns', 0), ('source', {'callback': object()})):
      changed = deepcopy(rows)
      changed[0][field] = value
      with self.subTest(field=field), self.assertRaises((TypeError, ValueError)):
        prepare_probe_payloads(changed, queued_ns=11_000_000_000, expected=context)
    with self.assertRaises(ValueError):
      prepare_probe_payloads(rows, queued_ns=11_000_000_000, expected={'batch': ()})


@pytest.mark.parametrize('elapsed', [2_000_000_001, 7_000_000_000, NUMERICAL_DEADLINE_NS])
def test_preparation_can_finish_after_two_seconds_without_renewing_the_numerical_deadline(elapsed):
  # Deliberately transport-only: accepting this envelope does not qualify a
  # native replay or a live offer. The independent worker still must do that.
  context = _prepared_context({'batch': payloads()})
  prepared = prepare_probe_payloads(payloads(), queued_ns=11_000_000_000, expected=context)
  process = ProbeProcess()
  with patch('openpilot.starpilot.flm.fit_process.FitProcess.submit_bytes', return_value=True) as submit:
    assert process.submit_prepared_probe(prepared, expected=context, token=('original',), now_ns=11_000_000_000 + elapsed)
  submit.assert_called_once()
  assert process.started_ns == 11_000_000_000
  assert process.wall_ns == NUMERICAL_DEADLINE_NS
  assert process.expected is not None
  assert process.expected[1] == context


@pytest.mark.parametrize('elapsed', [-1, NUMERICAL_DEADLINE_NS + 1])
def test_preparation_cannot_submit_before_original_enqueue_or_after_original_deadline(elapsed):
  context = _prepared_context({'batch': payloads()})
  prepared = prepare_probe_payloads(payloads(), queued_ns=11_000_000_000, expected=context)
  with patch('openpilot.starpilot.flm.fit_process.FitProcess.submit_bytes') as submit:
    assert not ProbeProcess().submit_prepared_probe(prepared, expected=context, token=(), now_ns=11_000_000_000 + elapsed)
  submit.assert_not_called()


def test_delayed_preparation_still_rejects_changed_parent_source_context():
  context = _prepared_context({'batch': payloads()})
  prepared = prepare_probe_payloads(payloads(), queued_ns=11_000_000_000, expected=context)
  changed = deepcopy(context)
  changed['batch'][0]['binding'] = {'identity': 'different'}
  with patch('openpilot.starpilot.flm.fit_process.FitProcess.submit_bytes') as submit, pytest.raises(ValueError, match='original context'):
    ProbeProcess().submit_prepared_probe(prepared, expected=changed, token=(), now_ns=18_000_000_000)
  submit.assert_not_called()
