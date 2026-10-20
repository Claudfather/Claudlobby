---
name: account-relog
description: "Use when an account must be switched, or a credential has expired. Re-authenticates the Claude Code account a fleet runs on, from a throwaway tmux server. Establishes blast radius first (a shared credential makes this a cross-fleet action), relays the device-login URL to the human, and verifies with `claude auth status` rather than the success message. Re-authenticating the SAME account does not reset a weekly limit."
allowed-tools: Bash(claude auth*), Bash(tmux *), Bash(grep *), Read, mcp__plugin_telegram_telegram__reply
argument-hint: "[--same-account]"
---

# Account Relog

Re-authenticate the account Claude Code runs as, without disturbing a single running bot. The whole procedure happens on a **throwaway tmux server** — never a worker pane, never the manager's.

**Two facts that change how urgent this is. State them to the human before they start rearranging their evening.**

- **There is no downtime window.** The existing credential stays valid until the new code lands. Bots keep working throughout, and if the human never finishes the flow, nothing breaks — you are back where you started. Nobody needs to stop work for this. The one thing an account change does drop is Remote Control, in every running session; Step 9 restores it.
- **The login flow does not consume model quota.** An exhausted account can still be re-authenticated. What an exhausted account prevents is *the operator being able to talk anyone through it* — so if quota is gone, expect the human to be driving from a phone with no working assistant on the other end. Write your messages accordingly: complete, self-contained, one step at a time.

## When this does and does not help

**A relog only gives fresh quota if it lands on a DIFFERENT account.** Weekly limits are enforced against the *account*, not the session or the machine. Re-authenticating the same account is a no-op for quota — it fixes an expired or broken credential and nothing else.

Switching to a different account changes **billing and identity for every bot sharing the credential**. That is a decision for the human, not for you. Establish which case you are in (Step 3) before anything is typed.

Pass `--same-account` when the intent is repairing a broken credential rather than switching. It does not change the procedure; it records the intent so Step 3 does not read as an open question.

## Steps

### 1. Pre-check — record the current account before touching anything

```bash
claude auth status
```

Read-only. Returns JSON: `loggedIn`, `authMethod`, `apiProvider`, `email`, `orgId`, `orgName`, `subscriptionType`.

Record `email`, `orgName` and `subscriptionType` **now**. This is the only cheap moment to learn what you are replacing — afterwards the old values are gone, and "which account was it before?" becomes unanswerable from the host.

### 2. Establish blast radius — MANDATORY, not optional

```bash
# host_bots_dirs resolves root, flat AND nested layouts. Do not re-derive the
# path shape here — that is the whole defect class (#920). It is sourced in a
# subshell because lib-common.sh arms `set -euo pipefail` at source time, which
# must not follow you into this shell. $sources is deliberately UNQUOTED on the
# grep line: those are globs and must expand. `/dev/null` keeps grep off stdin
# if the resolver ever returns nothing.
sources=$(. "$CLAUDLOBBY_NATIVE_DIR/lib-common.sh" >/dev/null 2>&1; \
          host_bots_dirs | sed 's:$:/*/bot.conf:')

# A resolver that returned nothing has queried nothing. That is a TOTAL failure,
# not an empty estate, and it must not come out the far side reading COMPLETE.
if [ -z "$sources" ]; then
  relog_rc=2
else
  grep -Hn 'CLAUDE_CONFIG_DIR' $sources /dev/null; relog_rc=$?
fi

# grep exits 2 if ANY source could not be opened — independently of how many
# real matches it also returned. A partial read therefore looks exactly like a
# complete one in the output alone, so the status is the only thing that can
# tell them apart. Capture it HERE, on the bare command, and print the verdict:
# the next thing you will reach for is a count, a count is a pipe, and a pipe
# replaces $? with the LAST stage's status — `| wc -l` over a failed read
# reports 0. Print it and there is nothing left to remember.
if [ "$relog_rc" -ge 2 ]; then
  printf 'INCOMPLETE — at least one source could not be read (grep rc=%s).\n' "$relog_rc"
else
  printf 'COMPLETE — every source was read (grep rc=%s).\n' "$relog_rc"
fi
```

**Read the verdict line first.** It is the only thing that tells you whether the
hits above it are the whole picture — a source that was never queried is silent,
and silence looks identical to a bot that simply does not set the variable. Then
read the hits, because **the line existing is not the same as the setting being
active**:

| What you see | What it means |
|---|---|
| **`INCOMPLETE`** | **At least one source was never queried, and real hits may sit right above it.** Those hits are a **subset**. The ordinary cause is a `runtime/bots/` that exists but holds no bot — a freshly scaffolded fleet, or one whose last canary was just torn down — which passes every guard in the command and still contributes nothing. **Stop here.** A blast radius derived from a partial read is an undercount delivered with full confidence, which is the single outcome this step exists to prevent. Fix the query before going near Step 3. |
| **`COMPLETE`**, but no hits at all | **The query resolved and matched nothing.** Any live host has bots, so on a real estate treat this as a failure you have not identified yet rather than as "nothing is configured" — verify by hand that the bot dirs are where you think they are. **Stop here** either way: you have no count and no fleet names, which is exactly what the rest of this step requires you to report. |
| **`COMPLETE`**, and every hit is **commented out** (`# CLAUDE_CONFIG_DIR=...`) | Every bot on the host shares **one keychain credential**, across **all fleets**. A relog re-authenticates all of them at once. |
| **`COMPLETE`**, and a bot sets it to its own path, uncommented | That bot has an isolated credential and is unaffected by a relog of the default. |

Do not skip this because you only manage one fleet. The shared case is the default and it is **cross-fleet by construction** — the credential is per *host* (and per OS user), not per fleet. A relog run without this check is a change to bots whose managers do not know it is happening.

If the credential is shared, say so explicitly to the human before proceeding, with the count and the fleet names, and hold Step 11 open.

### 3. Confirm which account, and why it matters

Tell the human, in one message: the account you found in Step 1, the account they intend to end on, the plan each carries (Max or Team, for example; Step 1's `subscriptionType` gives the current one), and the consequence of each. Name both plans, because usage limits differ by plan.

- **Same account** → the credential is refreshed. **Quota is not.** If they are relogging to escape a weekly limit, this will not do it, and this is the moment to say so — not after.
- **Different account** → fresh quota under the limits of that account's plan, **and** billing plus identity move for every bot found in Step 2.

Get an explicit answer. Do not infer it from the fact that they asked.

### 4. Run it in a throwaway tmux server

```bash
# The browsers running now, for Step 10 to compare against. bash -c: lib-common.sh
# is bash, and a zsh tool shell does not load it (#1549). Linux only; macOS prints nothing.
bash -c '. "$CLAUDLOBBY_NATIVE_DIR/lib-common.sh" >/dev/null 2>&1; python3 "$CLAUDLOBBY_NATIVE_DIR/browser-scan.py" --pattern "${BROWSER_EXE_PATTERN:?lib-common.sh did not load}" --min-age 0'

tmux -L relog new-session -d
tmux -L relog send-keys 'env -u DISPLAY WAYLAND_DISPLAY= BROWSER=true claude auth login' Enter
```

`-L relog` is a **separate tmux server**, not a session on an existing one.

- **No browser on the host.** On a host with a desktop session, a shell inherits `DISPLAY` and `WAYLAND_DISPLAY` with `BROWSER` unset. From there `claude auth login`, like a `gh` or `vercel` login, has xdg-open start a GUI Chromium that holds about 600 MiB (seen 2026-10-13 and 2026-10-19) and outlives both the CLI and Step 10, which stops only the tmux server (#2332). The `env` gives the login no display and a browser that does nothing, so it prints its URL and opens no window. It sets `WAYLAND_DISPLAY` empty rather than unsetting it: unset, a Wayland client falls back to the default socket and still reaches the desktop. A bot session already runs this way; the `env` keeps it so from any shell.

  Measured 2026-10-20: `env -u DISPLAY -u WAYLAND_DISPLAY BROWSER=/bin/true claude auth login` inside the throwaway tmux server still printed the URL, accepted the pasted code (`Login successful.`), and started 0 Chromium processes (checked before and after).

- **Not a worker pane.** It interrupts that worker mid-task and bakes the whole auth flow into its transcript, where it stays.
- **Not the manager pane.** Same problem, plus you lose the pane you are coordinating from.
- A private server is also trivially disposable in Step 10 — killing it cannot take a bot with it.

### 5. Extract the URL with `-J`

```bash
tmux -L relog capture-pane -p -J
```

**`-J` joins wrapped lines.** Without it the login URL is split mid-token across the pane width and you will be tempted to reassemble it by hand. Do not — a device-login URL carries a state parameter, and a hand-reassembled one fails in a way that looks like the human clicking wrong. `-J` costs one character and removes the entire class.

### 6. Relay the URL, and tell the human what to check on the page

Send the URL over whatever channel they are already on.

Include this, every time: **check the sign-in page shows the account you intend to end on, before approving.** A browser already signed in as the old account will happily authorise the flow, and the result is a successful-looking relog that changed nothing. From the host side that outcome is **invisible** — Step 1 and Step 8 return the same values, and it reads as a no-op rather than a mistake. The browser is the only place that distinction is visible, so the human is the only one who can catch it.

### 7. Enter the code

```bash
tmux -L relog send-keys '<code>'
sleep 1
tmux -L relog send-keys Enter
```

**Enter goes as a separate call, after a settle.** Sending the code and Enter in one `send-keys` submits before the TUI has registered the input, and the keystroke is lost — the same race `claudlobby/_runtime_scripts/lib-common.sh::pane_send_verified` exists to handle for bot startup. Here it is a hand-driven one-off, so the settle is manual.

**Never store, log, or echo the code.** Do not write it to a file, do not put it in a report, do not repeat it back on the channel. It is a short-lived credential-exchange token — it belongs in exactly one `send-keys` and nowhere else.

### 8. Verify with `claude auth status`, not with the success message

```bash
claude auth status
```

`Login successful` is the flow reporting on itself. `claude auth status` is a separate read of the resulting state, and it is the one that can contradict you. Compare `email` and `orgName` against what Step 3 agreed on.

If they match the *old* account, the browser-already-signed-in case from Step 6 happened. Say so plainly and offer to re-run — do not report success.

### 9. Probe the peer managers, and restore Remote Control

Ask each peer manager to reply with one line after its next turn runs on the new login. That probe worked on 2026-10-20.

Running sessions are expected to survive a relog — but "expected to" is not "observed to", and the cost of checking is one message each. When they answer, no restarts are needed and you should say that explicitly, because the assumption in the room will be that everything must be bounced.

**An account change disconnects Remote Control** in every running session. The pane reads `Remote Control disconnected — signed-in claude.ai account or organization changed on this machine — run /remote-control`. The session keeps working; only Remote Control drops. Restore it on the sessions meant to have it: the bots that run with `--remote-control` (fleet.yaml `remote_control`; by default only each fleet's manager).

Restoring it never types into a busy pane or a held box. What worked on 2026-10-20, on four managers:

1. Confirm the pane is idle and its input box empty.
2. Type `/remote-control` without Enter.
3. Confirm the box holds exactly that, its autocomplete row is highlighted, and the bot is still idle.
4. Press Enter once, and confirm `/remote-control is active`.

If any check fails, delete exactly what was typed. A manager cannot type into its own pane mid-turn, so its own restore runs after its turn ends.

Pitfalls measured the same day:

- The prompt glyph is followed by a no-break space (U+00A0), so an empty-box regex built on `[[:space:]]` never matches.
- Collapsed tool lines such as `… (41s · 4 lines)` look like a spinner.
- A pane that reads idle can start a turn a second later, so re-check right before Enter.
- Never send a plane message to a pane while typing into it.
- keepalive's `pane_state` reads a manager with background shells as BUSY (#2391), so a self-restore cannot wait on it.

### 10. Kill the throwaway server, and stop any browser the login started

```bash
tmux -L relog kill-server
```

Do this even if the flow failed. A stranded server holds a pane containing the auth exchange.

Then run Step 4's `browser-scan.py` line again. With Step 4's `env` the login starts no browser, so the list should hold nothing new. A row whose pid and start were not in Step 4's list is a browser this login started: stop it by its pid, typed as a number (`kill <pid>`), and list again to see it gone. Never stop a browser by name: `pkill chromium` stops every bot's browsers and renders, and the signal guard refuses it, as it refuses a pid read from `ps` or `pgrep` in the same command. Around any other login on the host (`gh auth login`, `vercel login`), do the same: list before, list after, stop only what the login started.

A browser left running on the host anyway, from this or any other path, appears in the next fleet pulse as `browser_outside_render`, with the bot whose session started it; the pulse reports it and never stops it.

### 11. Notify peer fleet managers

**Only if Step 2 found a shared credential** — which is the default case.

Tell every other fleet manager on the host: the account changed, from which to which, and that no restart was required. They did not initiate this and their bots' identity and billing just moved. A cross-fleet change nobody announced is indistinguishable to them from an unexplained account switch.

## Rules

- **Never automate the human half.** Opening the URL, choosing the account, and approving are the human's. This skill drives tmux and reads status; it does not drive a browser and does not decide which account to land on.
- **Never store or echo the login code**, anywhere, including in your own report.
- **Blast radius before credentials.** Step 2 is not optional and not reorderable — its answer determines whether this is a one-fleet action or a host-wide one, and that changes who must be told.
- **Verify by measurement.** Step 8 is `claude auth status`, never the success string. Same rule as everywhere else: a success message is a self-report.
- **Same account does not reset quota.** If that was the goal, say so before the human spends the effort.
- **No downtime.** Do not stop bots, do not warn of an outage, do not schedule a window. There is none: an account change drops only Remote Control, and Step 9 restores it.
- **Never put a real email, org ID, or account UUID into a committed file, an issue, or a PR.** Report them on the channel to the human who owns them.
