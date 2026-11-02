### Fixed — release automatic bootstrap after native activation

New activations hold automatic launcher startup through native readiness, then release recorded bot attempts after native active under selected-release, lifecycle and strict recipient locks. Each immutable session-bound attempt retains its payload and submission outcome; unknown or refused attempts are never replayed automatically. Managed session replacement uses the same recipient lock, while ordinary starts and older incomplete activations preserve their existing behavior. Native active and startup submission are reported separately; neither proves completed context restoration or business work.

Recipient capture and bound submission resolve the pane through tmux's exact-session pane target, retaining populated session and process identities on Linux.

Startup remains pending without recording an attempt when native input is blocked by a trust or permission prompt. After explicit approval, the same activation can continue its first send; previously unknown submissions are never replayed.

### Fixed — serialize timer jobs released after activation

Exact Linux timer services that waited for activation now share one mutex through their child bodies, within the existing admission deadline. Normal timer invocations and declared schedules are unchanged. Nested CLI admission remains available, and inherited descriptors retain both locks if a wrapper dies. No host-load threshold delays activation or startup.

### Fixed — resume can request another capture after a completed checkpoint expires

A native file approval can delay a requested checkpoint beyond the stop freshness window. Same-ID activation resume now preserves that verified expired checkpoint and its original unknown native receipt in history before issuing one new request through the existing handoff sender. Missing or predating captures, begun stops and blocked native input still refuse; the old checkpoint cannot authorize another send after the new request. Stop freshness checks remain unchanged.
