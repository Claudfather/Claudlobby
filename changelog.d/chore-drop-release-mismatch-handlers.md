### Removed — command branches for an error nothing raises

Nineteen commands caught `ReleaseMismatch`, which has had no raiser since sealed activation was retired, so those branches could not run and are gone. No command does or prints anything different: runtime admission refuses with `ActivationError`, and each of these commands already answers that with its own refusal. The class and the one handler in `commands/host_owner.py` stay for now.
