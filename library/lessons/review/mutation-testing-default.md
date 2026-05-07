---
title: Mutation Testing — Default, Not Special
description: A test that passes regardless of the code's behavior is a decoy. Mutate to detect.
---

A test passes for two reasons: the code is correct, or the test doesn't actually verify what it claims. The second case is a **decoy test** — it gives green in CI, ships in PRs, and hides bugs until they reach production.

Mutation testing detects decoys cheaply: remove the behavior the test claims to verify, re-run the test. If it still passes, the test is a decoy.

**The procedure:**

1. Read the test. Identify the **specific assertion** it makes ("the function returns the right ID for valid input").
2. In the implementation, **remove** the behavior that produces that result. Replace with a stub: `return None`, `return []`, `pass`, etc.
3. Re-run the test.
4. Test fails → real test, ship.
5. Test passes → decoy. Block the PR until the test is rewritten or removed.

**Common decoy patterns:**

- Test asserts on a side-effect that the implementation produces unconditionally.
- Test mocks the very function being tested, then asserts the mock was called.
- Test assertion is `assert result is not None` when the implementation always returns truthy.
- Test exercises only a happy path; the bug class lives in the unhappy paths.

**For data work specifically:**

- Test asserts row count > 0 when the model is non-empty by construction.
- Test asserts a column exists when the schema is enforced separately.
- Test uses a `.filter(...)` that always returns a row (decoy filter).
- Test runs against a fixture that includes the asserted state regardless of code change.

**Discipline:** mutation testing is **default**, not "for tricky PRs." Reviewers run it on every test claim that load-bears the PR. Cost is seconds; benefit is preventing a class of bugs that would otherwise ship green.
