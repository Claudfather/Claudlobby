### Fixed — a heavy-slot render test no longer fails when its poll reads the slot's lock file mid-write (#2866)

The render test's poll parsed `slot-0.lock` as it found it. `heavy-slot.py` creates that file empty and truncates it before each write, so a poll in that window failed `test_the_scope_dies_with_its_runner` with `JSONDecodeError` (CI run 42850852913). The test now reads the record as the module's own reader does, and as the run test has since #2125: an empty or half-written file is no record yet, and the poll tries again. Test-only; `heavy-slot.py` is unchanged.
