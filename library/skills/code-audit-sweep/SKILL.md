---
name: code-audit-sweep
description: "Run one automated code audit on a specific repo, dispatched by the rolling-sweep selector. Pulls latest main, runs the clauDNA audit with --auto --output github, guarantees the auto-audit staleness label, and emits an audit_completed event. Target repo and audit type are passed in — this skill does not pick them."
argument-hint: "<org/repo> <audit-type>"
---

# Code Audit Sweep

The execution half of the rolling code-audit sweep. The no-LLM selector
(`lib/code-audit-sweep.sh`) picks the stalest repo and the audit type for the
run, then dispatches `/code-audit-sweep <org/repo> <audit-type>` into this
bot's session. This skill runs exactly that one audit — it does **not** choose
the target (the selector already did, from live GitHub staleness).

The audit's filed issues, labelled `auto-audit`, ARE the staleness ledger the
next run reads. So the one non-negotiable post-condition is: **every issue this
run creates carries the `auto-audit` label.** Without it, the repo looks
never-audited and gets re-selected forever — the exact silent-miss this design
exists to kill.

## Arguments

`$ARGUMENTS` is `<org/repo> <audit-type>`, e.g. `my-org/repo-a tech-debt`.

- `<org/repo>` — the single repo to audit. Never cross repo boundaries.
- `<audit-type>` — one of the clauDNA audit skills below.

| audit-type | Skill run | Finds |
|---|---|---|
| `tech-debt` | `/claudna:audit tech-debt` | Dead code, god modules, deprecated patterns |
| `security-audit` | `/claudna:audit security` | Credential leaks, injection, auth gaps |
| `docs-review` | `/claudna:audit docs` | Stale or missing documentation |
| `data-model-audit` | `/claudna:audit data-model` | Schema / app mismatches |
| `product-enhance` | `/claudna:product-enhance` | UX gaps, missing features |

## Steps

**1. Pull latest main.** Audit current code, not a stale checkout.

```bash
cd "{{CLAUDLOBBY_ROOT}}/local/{{FLEET_NAME}}/runtime/bots/{{BOT_NAME}}/projects/<repo-name>"
git checkout main && git pull --ff-only
```

`<repo-name>` is the part of `<org/repo>` after the `/`. If the checkout is
missing, clone it under `projects/` first, or report the gap and stop — do not
fabricate an audit.

**2. Run the audit in a subagent** (keep your main context clean):

> Run `/claudna:<audit-type> --auto --output github`, scoped to the
> highest-impact directory of the repo. Cap at ~10 new issues. Capture every
> GitHub issue URL and number created.

**3. Guarantee the `auto-audit` label — do not trust delegation.**

`/claudna:publish` applies only the labels present in each doc's frontmatter
`tags:` (`gh issue create --label "<tags>"`). The audit skills
(`tech-debt`/`security-audit`/`product-enhance`) name only `priority:*` /
`enhancement` in their own output instructions; the "always apply `auto-audit`"
rule lives in the inherited `_shared/output-guide.md`, which an `--auto` run may
or may not honour. So treat the label as **your** responsibility:

- **Preferred:** if you can influence the audit's doc frontmatter, ensure
  `auto-audit` is in `tags:` of every doc *before* publish runs.
- **Always do this (the belt-and-suspenders guarantee):** right after the
  audit, label every issue it created this session. Record `started_at` (an ISO
  timestamp) before step 2, then:

```bash
gh issue list --repo <org/repo> --state open --limit 50 \
  --json number,createdAt,labels \
  -q '.[] | select(.createdAt >= "<started_at>") | select((.labels // []) | any(.name=="auto-audit") | not) | .number' \
| while read -r n; do gh issue edit "$n" --repo <org/repo> --add-label auto-audit; done
```

If `gh` errors (auth/rate-limit/network), report it verbatim and emit
`audit_failed` — never claim issues were labelled when you could not verify.

**4. Emit `audit_completed`** to this bot's event stream (closes the
observability loop the selector opened with `audit_selected`):

```bash
EVENTS="{{CLAUDLOBBY_ROOT}}/local/{{FLEET_NAME}}/runtime/bots/{{BOT_NAME}}/data/events/fleet-$(date +%Y-%m-%d).jsonl"
mkdir -p "$(dirname "$EVENTS")"
printf '{"ts":"%s","bot":"{{BOT_NAME}}","type":"audit_completed","source":"audit","data":{"repo":"<org/repo>","audit_type":"<audit-type>","issues":<count>}}\n' \
  "$(date -Iseconds)" >> "$EVENTS"
```

On any unrecoverable failure, emit `audit_failed` with the same shape plus a
`"reason"` field instead.

## Rules

- One repo per run — the one passed in. Never widen scope.
- Always `--auto` — this is unattended.
- Run the audit in a subagent, not your main context.
- Cap at ~10 new issues per run to avoid flooding.
- The `auto-audit` label is the post-condition. If you can't guarantee it,
  the run failed — say so.

$ARGUMENTS
