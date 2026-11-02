### Fixed — release automatic bootstrap after native activation

New activations hold automatic launcher startup through native readiness, then release recorded bot attempts after native active under selected-release, lifecycle and strict recipient locks. Each immutable session-bound attempt retains its payload and submission outcome; unknown or refused attempts are never replayed automatically. Managed session replacement uses the same recipient lock, while ordinary starts and older incomplete activations preserve their existing behavior. Native active and startup submission are reported separately; neither proves completed context restoration or business work.

Recipient capture and bound submission resolve the pane through tmux's exact-session pane target, retaining populated session and process identities on Linux.
