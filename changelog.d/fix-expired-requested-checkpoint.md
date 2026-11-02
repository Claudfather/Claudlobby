### Fixed — resume can request another capture after a completed checkpoint expires

A native file approval can delay a requested checkpoint beyond the stop freshness window. Same-ID activation resume now preserves that verified expired checkpoint and its original unknown native receipt in history before issuing one new request through the existing handoff sender. Missing or predating captures, begun stops and blocked native input still refuse; the old checkpoint cannot authorize another send after the new request. Stop freshness checks remain unchanged.
