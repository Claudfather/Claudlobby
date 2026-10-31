---
title: Secret Incidents
description: How a bot equipped with GitGuardian triages secret incidents without holding a secret, and where its findings go
---

# Secret Incidents

GitGuardian records each secret it detects as an incident. You read incidents through the `gitguardian` skill; the operator changes them.

**On every step:**
1. **No secret in your hands.** Reach GitGuardian only through `.claude/skills/gitguardian/gitguardian_api.sh`, never with a hand-written `curl` or another client. Never open a detected secret's lines yourself, and never print the token.
2. **Nothing exploitable leaves the session.** Reports, messages, issues and docs carry incident numbers and class words only: no repository, path, line, commit, hash or value.
3. **Readiness first.** Start each pass with `status`. Any answer but `reachable` is a block: report it with that word, and never retry with another token.
4. **No changes.** The helper refuses every write. The operator closes, resolves and reopens incidents in GitGuardian's dashboard.

**A pass.** Run `incidents`, then `triage ID` for each open incident, and act on its class:

| Class | When | You |
|---|---|---|
| `live` | GitGuardian reads it `valid`, or this session holds the same value | Stop, and touch nothing else. Tell the operator by direct message, never a group, GitHub or a doc |
| `false_positive` | a placeholder, and not `valid` | List it for the operator to close as a false positive |
| `needs_operator` | anything else | List it with its evidence words |

Then send the operator one direct message: the incident numbers grouped by class, each with its dashboard link from `incident ID`.

**What the operator does with a detected secret.** Your part ends at that message.
1. Learn whether it is live, from GitGuardian's validity or at the provider. Nobody tries a found credential.
2. If it is live, rotate it at the provider, update the stored value, and restart each bot that uses it: a session reads `.env` only when it starts. Removing it from git history is a separate, destructive decision.
3. Resolve the incident as revoked once it is rotated.
4. Close an incident as a false positive only on the `false_positive` evidence. Closing one as a test credential, as low risk or as invalid is the operator's judgment alone.
