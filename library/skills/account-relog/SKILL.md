---
name: account-relog
description: "Re-authenticate the Claude Code account a fleet runs on, from a throwaway tmux server. Establishes blast radius first (a shared credential makes this a cross-fleet action), relays the device-login URL to the human, and verifies with `claude auth status` rather than the success message. Use when an account must be switched, or a credential has expired. Re-authenticating the SAME account does not reset a weekly limit."
allowed-tools: Bash(claude auth*), Bash(tmux *), Bash(grep *), Read, mcp__plugin_telegram_telegram__reply
argument-hint: "[--same-account]"
---

# Account Relog

Re-authenticate the account Claude Code runs as, without disturbing a single running bot. The whole procedure happens on a **throwaway tmux server** — never a worker pane, never the manager's.

**Two facts that change how urgent this is. State them to the human before they start rearranging their evening.**

- **There is no downtime window.** The existing credential stays valid until the new code lands. Bots keep working throughout, and if the human never finishes the flow, nothing breaks — you are back where you started. Nobody needs to stop work for this.
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
grep -rn 'CLAUDE_CONFIG_DIR' "$CLAUDLOBBY_ROOT"/local/*/runtime/bots/*/bot.conf
```

Read the result carefully — **the line existing is not the same as the setting being active**:

| What you see | What it means |
|---|---|
| Every hit is **commented out** (`# CLAUDE_CONFIG_DIR=...`) | Every bot on the host shares **one keychain credential**, across **all fleets**. A relog re-authenticates all of them at once. |
| A bot sets it to its own path, uncommented | That bot has an isolated credential and is unaffected by a relog of the default. |

Do not skip this because you only manage one fleet. The shared case is the default and it is **cross-fleet by construction** — the credential is per *host* (and per OS user), not per fleet. A relog run without this check is a change to bots whose managers do not know it is happening.

If the credential is shared, say so explicitly to the human before proceeding, with the count and the fleet names, and hold Step 11 open.

### 3. Confirm which account, and why it matters

Tell the human, in one message: the account you found in Step 1, the account they intend to end on, and the consequence of each.

- **Same account** → the credential is refreshed. **Quota is not.** If they are relogging to escape a weekly limit, this will not do it, and this is the moment to say so — not after.
- **Different account** → fresh quota, **and** billing plus identity move for every bot found in Step 2.

Get an explicit answer. Do not infer it from the fact that they asked.

### 4. Run it in a throwaway tmux server

```bash
tmux -L relog new-session -d
tmux -L relog send-keys 'claude auth login' Enter
```

`-L relog` is a **separate tmux server**, not a session on an existing one.

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

**Enter goes as a separate call, after a settle.** Sending the code and Enter in one `send-keys` submits before the TUI has registered the input, and the keystroke is lost — the same race `lib/lib-common.sh::pane_send_verified` exists to handle for bot startup. Here it is a hand-driven one-off, so the settle is manual.

**Never store, log, or echo the code.** Do not write it to a file, do not put it in a report, do not repeat it back on the channel. It is a short-lived credential-exchange token — it belongs in exactly one `send-keys` and nowhere else.

### 8. Verify with `claude auth status`, not with the success message

```bash
claude auth status
```

`Login successful` is the flow reporting on itself. `claude auth status` is a separate read of the resulting state, and it is the one that can contradict you. Compare `email` and `orgName` against what Step 3 agreed on.

If they match the *old* account, the browser-already-signed-in case from Step 6 happened. Say so plainly and offer to re-run — do not report success.

### 9. Probe a running bot

Send a trivial turn to one live bot and confirm it answers.

Live sessions hold their credential in memory and are expected to survive a relog untouched — but "expected to" is not "observed to", and the cost of checking is one message. If it answers, no restarts are needed and you should say that explicitly, because the assumption in the room will be that everything must be bounced.

### 10. Kill the throwaway server

```bash
tmux -L relog kill-server
```

Do this even if the flow failed. A stranded server holds a pane containing the auth exchange.

### 11. Notify peer fleet managers

**Only if Step 2 found a shared credential** — which is the default case.

Tell every other fleet manager on the host: the account changed, from which to which, and that no restart was required. They did not initiate this and their bots' identity and billing just moved. A cross-fleet change nobody announced is indistinguishable to them from an unexplained account switch.

## Rules

- **Never automate the human half.** Opening the URL, choosing the account, and approving are the human's. This skill drives tmux and reads status; it does not drive a browser and does not decide which account to land on.
- **Never store or echo the login code**, anywhere, including in your own report.
- **Blast radius before credentials.** Step 2 is not optional and not reorderable — its answer determines whether this is a one-fleet action or a host-wide one, and that changes who must be told.
- **Verify by measurement.** Step 8 is `claude auth status`, never the success string. Same rule as everywhere else: a success message is a self-report.
- **Same account does not reset quota.** If that was the goal, say so before the human spends the effort.
- **No downtime.** Do not stop bots, do not warn of an outage, do not schedule a window. There is none.
- **Never put a real email, org ID, or account UUID into a committed file, an issue, or a PR.** Report them on the channel to the human who owns them.
