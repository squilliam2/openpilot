"""Source-clock exclusion of driver input, without rejecting a recording."""

from bisect import bisect_left

BEFORE_NS = 350_000_000
AFTER_NS = 1_000_000_000
MAX_INTERVALS = 16384


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
