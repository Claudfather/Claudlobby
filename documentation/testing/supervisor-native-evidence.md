# Supervisor lifecycle native evidence

The action-adoption refactor in [#1862](https://github.com/Claudfather/Claudlobby/pull/1862)
was exercised on disposable Linux and macOS hosts in
[run 36246470151](https://github.com/Claudfather/Claudlobby/actions/runs/36246470151):

- Parent: `cb41997969274c77c7b6ed39ef171f22d1ccbb70`.
- Candidate: `8201e2380b85470e2d16406b581253c5e81d95a5`.
- Both revisions passed the complete validation harness: Linux 344/0; macOS 320/0.
- Both passed all nine native lifecycle cases, scratch receipt recording,
  preservation checks, negative controls, and cleanup.

The dedicated workflow and driver were one-time acceptance tooling. They remain
available in the candidate commit above; they are no longer maintained as a
second test framework. Removing them changes no production code. These results
belong to the recorded revisions; the simplified PR head has not repeated the
native run.

Recurring regression coverage remains in `tests/test_supervisor_lifecycle.sh`,
`tests/test_supervisor_adapter.sh`, `tests/test_spin_down_receipt.sh`, the lifecycle
identity tests, and `tests/test_supervisor_ratchet.py`. Pytest discovers the shell
suites through `tests/test_sh_suites.py`. Phase 03's standard platform CI adds
native service coverage through `tests/test_macos_supervision.py`; that is separate
from the recorded parent/candidate acceptance run. Future runtime changes still
require relevant empirical evidence under the repository's normal validation
rules. This record neither closes the broader #1607 program nor authorizes rollout.
