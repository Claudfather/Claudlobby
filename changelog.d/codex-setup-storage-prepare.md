### Added — prepare connected workspace storage in one local operation

`host owner initialize-connected --prepare-workspace` can prepare both optional storage schemas atomically. It shows both preparations and requires the existing `INITIALIZE-WORKSPACE` and `INITIALIZE-CONNECTED` confirmations before changing storage. Cancellation, changed owner identity or an incompatible schema leaves preparation unapplied. Existing commands and defaults remain supported; this does not approve a workspace, create a session or grant fleet actions.
