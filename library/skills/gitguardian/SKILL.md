---
name: gitguardian
description: "Use when a bot equipped with the gitguardian integration checks its GitGuardian access, lists the open secret incidents, or triages one for the secret-incidents protocol. Calls GitGuardian's REST API through gitguardian_api.sh, which sends only GET requests on the incident routes and refuses every write before a request, whatever the token's scopes. It prints no token, no response body and nothing that locates a detected secret."
argument-hint: "[status|incidents|incident <id>|triage <id> [--checkout DIR]...|help]"
tool_grants:
  - "Bash(.claude/skills/gitguardian/gitguardian_api.sh status *)"
  - "Bash(.claude/skills/gitguardian/gitguardian_api.sh incidents *)"
  - "Bash(.claude/skills/gitguardian/gitguardian_api.sh incident *)"
  - "Bash(.claude/skills/gitguardian/gitguardian_api.sh triage *)"
  - "Bash(.claude/skills/gitguardian/gitguardian_api.sh help *)"
---

# GitGuardian

Reads GitGuardian's secret incidents for the Secret Incidents protocol, through `gitguardian_api.sh`. It needs `GITGUARDIAN_API_KEY` in this session (the integration declares it), `curl` and `jq`, and `python3` for `triage`.

```bash
.claude/skills/gitguardian/gitguardian_api.sh status          # may this token list incidents? one request
.claude/skills/gitguardian/gitguardian_api.sh incidents       # the open incidents, one line each
.claude/skills/gitguardian/gitguardian_api.sh incident 123    # one incident, with its dashboard link
.claude/skills/gitguardian/gitguardian_api.sh triage 123      # evidence words and a class
.claude/skills/gitguardian/gitguardian_api.sh help
```

## Each answer's first word

| Word | Exit | Meaning | You |
|---|---|---|---|
| `reachable` | 0 | GitGuardian answered with incidents | Go on |
| `missing` | 2 | `GITGUARDIAN_API_KEY` is unset or empty in this session; no request was sent | Block. The answer names the `config explain` command that shows which tier lacks it |
| `rejected` | 3 | HTTP 401: the token was not accepted | Block. Never retry with another token |
| `insufficient_scope` | 4 | HTTP 403: the token was accepted but may not read incidents | Block |
| `no_answer` | 5 | a timeout, a network failure, a 429 or a 5xx: nothing is known about the token | Try once more later, then block |
| `unknown` | 6 | any other answer, with its HTTP status | Block, quoting the line |
| `refused` | 7 | not sent: a write, a host other than GitGuardian's, or a key with a character no token has | Leave the change to the operator, or fix the setting it names |
| `absent` | 69 | `curl`, `jq` or `python3` is not installed | Block |

`incidents` and `incident` print one line per incident: its number, status, validity, severity, occurrence count, dashboard link and detector. `incidents` ends with the count, and says when the list is incomplete.

## triage

`triage ID` reads one incident with up to 100 occurrences and prints one line:

```
triage: incident=123 class=false_positive validity=no_checker placeholder=yes ours=no path_class=fixture occurrences=1/1
```

- `validity`: GitGuardian's own check, `valid`, `invalid`, `failed_to_check`, `no_checker` or `unknown`. Nobody tries a found credential to learn more.
- `placeholder`: `yes` when the secret is a placeholder (`REPLACE_ME`, or a run of one repeated character after a short prefix, as in `ghp_xxxxxxxx`), else `no` or `cannot_check`.
- `ours`: the name of each variable in this session's environment that holds the secret, else `no` or `cannot_check`. Never its value.
- `path_class`: `test`, `fixture`, `docs` or `source`, for each occurrence read.
- `class`: `live`, `false_positive` or `needs_operator`. The Secret Incidents protocol says what each asks of you.

For `placeholder`, triage reads the secret's lines from a local checkout that holds the commit: each git checkout under `projects/`, or each `--checkout DIR` you name. It takes the match from those lines only when the incident's HasMySecretLeaked hash confirms it, and otherwise reads `cannot_check`, with a `note=` word that says why (`no_local_commit`, `no_match` and others).

## Permissions

The `tool_grants` above allow the read commands in the form shown, so they run without a prompt. The helper has no write: `ignore`, `resolve` and `reopen` print `refused` and send nothing, and the operator makes every change in GitGuardian's dashboard.

- **Run it from the bot directory.** The grants name a path relative to it.
- **Write values out.** Claude Code allows a command that contains a `$VARIABLE` only through an exact-text rule, so write an incident number or a `--checkout` path literally.

## Never

- Never print, echo or pass on `GITGUARDIAN_API_KEY`. The helper is its only reader.
- Never reach GitGuardian another way: no hand-written `curl`, no other client.
- Never copy a repository, path, line, commit or hash from GitGuardian into a report, a message or a doc: incident numbers and class words only. Only the operator's direct message may also carry each incident's dashboard link.
