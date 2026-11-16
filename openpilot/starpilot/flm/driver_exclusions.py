"""Source-clock exclusion of driver input, without rejecting a recording."""

from bisect import bisect_left
from collections import deque
import math

BEFORE_NS = 350_000_000
AFTER_NS = 1_000_000_000
MAX_INTERVALS = 16384
STEADY_PEDAL_NS = 1_000_000_000
PEDAL_SOURCE_AGE_NS = 100_000_000
MAX_STEADY_ACCEL = 0.5
MAX_PEDAL_STEP = 0.02
MAX_PEDAL_RANGE = 0.05
MAX_ACCEL_STEP = 0.15
MAX_ACCEL_RANGE = 0.6
MAX_ACCEL_RATE = 0.75


class AolPedalPolicy:
  """Steady AOL accelerator use is not a lateral driver override.

  This numerical source-clock gate creates no authority or accepted tune.
  Steering/brake/regen and ordinary engaged accelerator use remain excluded.
  Current CarState records gasPressed and aEgo, not pedal position. Continuous
  pressed state plus quiet original acceleration earns the AOL exception;
  unseen pedal magnitude cannot be detected before its longitudinal response.
  Numeric pedal position, when genuinely supplied, adds independent checks.
  """

  def __init__(self):
    self.accepted_gas_rows = self.excluded_gas_rows = 0
    self.numeric_pedal_position_rows = 0
    self.last_reason = 'initializing'
    self.reset()

  def reset(self):
    self.history = deque(maxlen=512)
    self.last_stamp = 0
    self.last_signature = None
    self.last_excluded = True
    self.input_stamp = 0
    self.input_signature = None
    self.input_gas = False
    self.input_excluded = True

  def _reject(self, reason, *, gas=False):
    self.reset()
    self.last_reason = reason
    self.excluded_gas_rows += bool(gas)
    return True

  def observe(self, cs, cc, *, state_ns, command_ns, state_valid=True, command_valid=True):
    if type(state_ns) is not int or state_ns <= 0:
      return self._reject('invalid_driver_input_clock')
    flags = tuple(getattr(cs, name, None) for name in ('steeringPressed', 'brakePressed', 'regenBraking', 'gasPressed'))
    gas = flags[-1] is True
    command_current = (command_valid is True and type(command_ns) is int and command_ns > 0 and
                       abs(state_ns - command_ns) <= PEDAL_SOURCE_AGE_NS)
    signature = (flags, state_valid, tuple(getattr(cc, name, None) for name in ('latActive', 'enabled', 'longActive')) if gas else None,
                 getattr(cs, 'gas', None) if gas else None, getattr(cs, 'aEgo', None) if gas else None, command_current if gas else None)
    if state_ns == self.input_stamp:
      if signature == self.input_signature:
        return self.input_excluded
      result = self._reject('changed_input_at_same_source_clock', gas=gas)
    elif self.input_stamp and state_ns < self.input_stamp:
      result = self._reject('driver_input_clock_regressed', gas=gas)
    else:
      released = self.input_gas and not gas and 0 < state_ns - self.input_stamp <= PEDAL_SOURCE_AGE_NS
      result = self._observe(cs, cc, state_ns=state_ns, command_ns=command_ns, state_valid=state_valid,
                             command_valid=command_valid, released=released)
    self.input_stamp, self.input_signature, self.input_gas, self.input_excluded = state_ns, signature, gas, result
    return result

  def _observe(self, cs, cc, *, state_ns, command_ns, state_valid, command_valid, released):
    try:
      flags = tuple(getattr(cs, name) for name in ('steeringPressed', 'brakePressed', 'regenBraking', 'gasPressed'))
    except AttributeError:
      return self._reject('unavailable_driver_input')
    if any(type(value) is not bool for value in flags) or state_valid is not True or type(state_ns) is not int or state_ns <= 0:
      return self._reject('invalid_driver_input')
    steering, brake, regen, gas = flags
    if steering or brake or regen:
      return self._reject('driver_override', gas=gas)
    if not gas:
      if released:
        return self._reject('accelerator_release_transient')
      self.reset()
      self.last_reason = 'no_accelerator_override'
      return False
    try:
      commands = tuple(getattr(cc, name) for name in ('latActive', 'enabled', 'longActive'))
      accel = cs.aEgo
      pedal = getattr(cs, 'gas', None)
    except AttributeError:
      return self._reject('unavailable_pedal_source', gas=True)
    if (command_valid is not True or any(type(value) is not bool for value in commands) or
        type(command_ns) is not int or command_ns <= 0 or abs(state_ns - command_ns) > PEDAL_SOURCE_AGE_NS):
      return self._reject('stale_or_invalid_aol_command', gas=True)
    if commands != (True, False, False):
      return self._reject('ordinary_accelerator_override', gas=True)
    if (pedal is not None and (type(pedal) not in (int, float) or not math.isfinite(pedal) or not 0 <= pedal <= 1) or
        type(accel) not in (int, float) or not math.isfinite(accel) or abs(accel) > MAX_STEADY_ACCEL):
      return self._reject('pedal_or_longitudinal_acceleration_transient', gas=True)
    signature = (flags, commands, pedal, accel)
    if state_ns == self.last_stamp:
      if signature == self.last_signature:
        return self.last_excluded  # Repeated receipts cannot advance source time.
      return self._reject('changed_input_at_same_source_clock', gas=True)
    if self.last_stamp and not 0 < state_ns - self.last_stamp <= PEDAL_SOURCE_AGE_NS:
      self.reset()
    if pedal is not None:
      self.numeric_pedal_position_rows += 1
    if self.history and ((pedal is None) != (self.history[-1][1] is None) or
                         pedal is not None and self.history[-1][1] is not None and
                         abs(pedal - self.history[-1][1]) > MAX_PEDAL_STEP + 1e-12 or
                         abs(accel - self.history[-1][2]) > MAX_ACCEL_STEP + 1e-12):
      self.reset()
    self.history.append((state_ns, None if pedal is None else float(pedal), float(accel)))
    while len(self.history) > 1 and self.history[1][0] <= state_ns - STEADY_PEDAL_NS:
      self.history.popleft()
    pedals = [row[1] for row in self.history if row[1] is not None]
    accelerations = [row[2] for row in self.history]
    acceleration_range = max(accelerations) - min(accelerations)
    if (pedals and max(pedals) - min(pedals) > MAX_PEDAL_RANGE + 1e-12 or
        acceleration_range > MAX_ACCEL_RANGE + 1e-12):
      self.reset()
      self.history.append((state_ns, None if pedal is None else float(pedal), float(accel)))
    ready = state_ns - self.history[0][0] >= STEADY_PEDAL_NS
    if ready:
      # Source-time endpoint means avoid treating normal 100 Hz estimator noise
      # as a physical jerk, while rejecting sustained acceleration transitions.
      first = [row for row in self.history if row[0] <= self.history[0][0] + 200_000_000]
      last = [row for row in self.history if row[0] >= state_ns - 200_000_000]
      elapsed = (sum(row[0] for row in last) / len(last) - sum(row[0] for row in first) / len(first)) / 1e9
      rate = abs(sum(row[2] for row in last) / len(last) - sum(row[2] for row in first) / len(first)) / elapsed
      if rate > MAX_ACCEL_RATE:
        self.reset()
        self.history.append((state_ns, None if pedal is None else float(pedal), float(accel)))
        ready = False
    self.last_stamp, self.last_signature, self.last_excluded = state_ns, signature, not ready
    self.last_reason = 'steady_aol_accelerator' if ready else 'aol_accelerator_settling'
    if ready:
      self.accepted_gas_rows += 1
    else:
      self.excluded_gas_rows += 1
    return not ready


def recorded_driver_exclusions(events):
  """Classify original CarState rows using only already recorded commands."""
  policy, excluded = AolPedalPolicy(), DriverExclusions()
  command = None
  decisions = {}
  for event in events:
    kind = event.which()
    if kind == 'carControl':
      command = event
    elif kind == 'carState':
      stamp = int(event.logMonoTime)
      decision = policy.observe(event.carState, None if command is None else command.carControl,
                                state_ns=stamp, command_ns=0 if command is None else int(command.logMonoTime),
                                state_valid=bool(event.valid), command_valid=command is not None and bool(command.valid))
      decisions[stamp] = decision
      if decision:
        excluded.observe(stamp)
  excluded.finish()
  return excluded, decisions, policy


class DriverExclusions:
  def __init__(self):
    self.intervals = []
    self.ends = None

  def observe(self, stamp):
    if type(stamp) is not int or stamp <= 0 or self.ends is not None:
      raise ValueError('Original driver-input clock required')
    start, end = stamp - BEFORE_NS, stamp + AFTER_NS
    if self.intervals and start <= self.intervals[-1][1] and end >= self.intervals[-1][0]:
      old = self.intervals[-1]
      self.intervals[-1] = (min(start, old[0]), max(end, old[1]))
    else:
      if len(self.intervals) >= MAX_INTERVALS:
        raise ValueError('Driver-input interval bound exceeded')
      self.intervals.append((start, end))

  def finish(self):
    merged = []
    for start, end in sorted(self.intervals):
      if merged and start <= merged[-1][1]:
        merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
      else:
        merged.append((start, end))
    self.intervals = merged
    self.ends = [end for _, end in merged]

  def overlaps(self, start, end):
    if self.ends is None:
      raise ValueError('Finish original driver-input collection first')
    index = bisect_left(self.ends, start)
    return index < len(self.intervals) and self.intervals[index][0] <= end
