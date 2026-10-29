---
title: Consequential actions need a recorded task permission
description: Bind merge, deploy, spend and destructive data actions to the core task permission owner.
permissions:
  allow:
    - "Bash(claudlobby task permission --help)"
    - "Bash(claudlobby --json task permission request *)"
    - "Bash(claudlobby --json task permission show *)"
    - "Bash(claudlobby --json task permission check *)"
    - "Bash(claudlobby --json request show *)"
---

# Consequential actions need a recorded task permission

Planning, delegation, editing, testing, review and task-branch PR work may proceed
within the human's approved project and goal. Before merging a PR, deploying,
spending beyond agreed provider usage or destructively changing data, obtain an
explicit permission through the canonical task owner. Review approval, a message,
a notification or a tool allowance is not that permission.

Keep the action's task open and its assignment unchanged until the proposed
effect has a confirmed outcome. A PR ready for review is a progress report on
that task, not completion. Review uses a separate task rather than reassigning
the permission-bearing task. If earlier work already closed, the manager must
admit and assign new follow-up work before requesting permission; never reopen
or reuse an old grant by implication.

For that open task, write a local JSON scope file containing exactly `action`
(`merge`, `deploy`, `spend` or `delete`), `target` and `constraints`. Name the exact
repository/PR and commit, deployment environment and revision, spending ceiling,
or deletion selection as applicable. Include no secrets. Before submitting, save
the exact scope, reason, task ID and request UUID under your bot-owned
`data/permission-requests/` directory so a restart cannot lose the request.
Record it with
`claudlobby --json task permission request TASK_ID --scope-file FILE --reason TEXT --request-id UUID`.
Save the returned permission ID there and in your handoff. If you are the
assignee, also reference it in your canonical progress report; a manager must
not submit a worker report under someone else's assignment. After a lost
response, inspect `claudlobby --json request show UUID`; if needed, repeat only
the identical request with its saved UUID and payload to recover the record.
Never generate another UUID to recover it.

Wait for the human's decision; continue independent authorized work meanwhile.
Read it with `claudlobby --json task permission show PERMISSION_ID`, including after
resuming. A decision notice names the record but grants nothing by itself; a
missing notice is not a decline. Immediately before
the proposed effect, the requesting bot must run
`claudlobby --json task permission check PERMISSION_ID --scope-file FILE` and require
success for the exact scope derived from the actual operation. Pending, declined,
revoked, unavailable or stale permission means stop that effect. Reassignment or
task closure invalidates approval; another bot cannot borrow the requester's grant.
Changed terms require a fresh request and decision, never a broader interpretation.

Enforce every stated constraint as well as repository review, CI, credentials,
role restrictions and other guardrails. Only the fleet manager may merge another
bot's PR after independent review and the human's recorded scope approval.
Authors never merge their own PRs; reviewers return verdicts and never merge.
No bot, including the manager, uses `--admin` or otherwise bypasses branch
protection. A blocked merge stays blocked until the unmet requirement is resolved.
A grant never permits out-of-scope work or disabling a safety control. If a constraint
cannot be checked or enforced, report the block instead of executing. Use existing
workflow and operation owners; the permission check itself executes nothing.
After an uncertain effect, inspect the provider's actual result before considering
any retry. Record confirmed, failed and unknown outcomes through the existing
report/message commands, preserving the task and permission IDs. Managers tell
the assignee the observed outcome so it can report against its own assignment.
A standing approval does not make repeated effects idempotent.

This is composed operating policy, not interception of arbitrary shell commands.
Do not claim that instruction text or a successful permission check mechanically
enforces a later external operation.
