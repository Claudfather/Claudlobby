### Fixed — wait for saved context and stop checkpointed bots cleanly

- Host activation waits for slow handoff saves without resending requests, reports progress, and keeps a timed-out wait resumable.
- Linux activation closes the checkpointed bot's private session before stopping its masked service, and reconciles a failed stopped bot only after checking its process state.
