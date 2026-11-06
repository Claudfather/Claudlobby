### Fixed — a host update's stage stops when no unit finishes, not after a fixed 900 s (#2734)

The limit on a stage process was 900 s, and restoring 21 bots takes about 1,040 s, so a healthy restore was cut and its timers left for another run. Quiesce and restore now run for as long as they keep finishing units, and stop when 1,200 s pass with none finished: the longest honest step is a bot's 900 s capture wait. A unit that records a restore error does not count, so a stage failing every unit still stops and tells someone. A stage with no units keeps a plain limit, now also 1,200 s.
