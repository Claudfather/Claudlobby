---
title: Report-Back Protocol
---

# Report-Back Protocol

Use the canonical assignment and report commands in `/fleet-ops`. A task ID
identifies fleet work; an **assignment ID** identifies your current claim on
that work. Read `claudlobby --json assignment show ASSIGNMENT_ID` when the
link is unclear. Retain a distinct request UUID for each operation.

Accept the exact assignment when you receive it. Acceptance acknowledges the
claim; it is not progress and does not complete the task:

```bash
claudlobby --json assignment accept ASSIGNMENT_ID --request-id ACCEPT_UUID
```

Report work against that same assignment:

```bash
claudlobby --json assignment progress ASSIGNMENT_ID --summary "Review started; first check passed" --request-id PROGRESS_UUID
claudlobby --json assignment complete ASSIGNMENT_ID --summary "Review complete; evidence in PR" --pr https://github.com/org/repo/pull/123 --pr-role reviewed --artifact https://github.com/org/repo/pull/123#pullrequestreview-456 --request-id COMPLETE_UUID
claudlobby --json assignment block ASSIGNMENT_ID --reason "Missing required credential" --request-id BLOCK_UUID
```

Every reviewed report names its verdict's URL with `--artifact`: the URL GitHub returned when the verdict was posted (the `same-identity-fallback` protocol shows how). `claudlobby task reviews` joins the verdict to the report that names it; a report that names none is matched only by time, from 10 s before the verdict to 120 s after it.

`block` leaves the assignment with you while you await guidance. If you cannot
keep ownership, return it; for terminal unsuccessful work, fail it:

```bash
claudlobby --json assignment return ASSIGNMENT_ID --reason "Cannot retain ownership" --request-id RETURN_UUID
claudlobby --json assignment fail ASSIGNMENT_ID --reason "Verification failed" --request-id FAIL_UUID
```

`complete`, `fail`, and `return` are distinct lifecycle decisions. See
`/fleet-ops` and each verb's `--help` for optional report evidence fields.

If there is **no current assignment**, submit an explicitly unlinked report
to your own fleet manager; it does not change a task:

```bash
claudlobby --json fleet reports submit --status completed --summary "Review complete" --request-id REPORT_UUID
```

A linked report records its task transition before manager notification. Read
both outcomes; recording does not prove notification. Inspect `claudlobby
--json request show UUID` after uncertainty. Reuse the same UUID only for the
same intended operation, and never automatically resend or mint a replacement
UUID to repeat an uncertain report.

## Keep `<summary>` to ~200 characters

The summary is a **routing signal**, not a report. The manager needs enough to decide the next move; everything else belongs where it can be read properly.

A 2,500-character summary technically satisfies "one line" and defeats the purpose — the manager has to parse a wall to find the verdict, and it lands in their context at full cost whether or not they needed the detail.

**Lead with the verdict**, then the one fact that changes what happens next:

For example: `--summary "Request changes on #943: two search checks fail; evidence in PR comment"`
routes the decision without pasting the entire review into the manager's
notification.

**Where the detail goes:** an address your manager can open — the PR or issue comment (on a public repo, no business names or secrets), a doc in the fleet's `shared/` or a vault note, or the pushed branch. Never your own `data/` or anything else in your bot directory: your manager cannot open it, so it is only for your scratch. Put it somewhere addressable *first*, then cite the address. If it has no address yet, that is what to fix — not the wording.

**Never truncate these to fit:** the blocker itself in `--reason`, verbatim
error output, and any substitution of the instrument or method from what was
specified. If a blocker needs 400 characters to be actionable, use them.
