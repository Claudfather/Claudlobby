---
name: finance
description: "Use when the user asks about finances, account balances, portfolio performance, transactions, or spending. Also used by /briefing for daily financial summaries."
argument-hint: "[portfolio|transactions|balances|spending] [options]"
---

# Finance

View portfolio performance, account balances, and transaction summaries via SimpleFIN.

## Environment Setup

SimpleFIN requires the `SIMPLEFIN_ACCESS_URL` env var, and it is already in your session: `start-bot.sh` sources the `.env` tiers at boot (`~/.env` among them), and every Bash tool call inherits the session environment. So never read `~/.env` yourself. Check the variable instead, so a missing one fails loudly here rather than as a SimpleFIN error later:

```bash
: "${SIMPLEFIN_ACCESS_URL:?not in the session env - declare it with export in a .env tier, then restart the bot}"
```

Run this before every finance command. It prints nothing when the variable is set, and never prints the value.

**Why not `source ~/.env`.** It loads nothing the session does not already hold, and it makes the skill depend on reading a secrets file through your own tools — a path an isolation rule may deny (#1665). A denied read there would drop the finance section from every briefing, and nothing records a denied tool call. A credential rotated in `~/.env` reaches the session at the bot's next restart, like every other composed secret.

## Scripts

All scripts are in `<ASSISTANT_TOOLS_DIR>/finances/`:

### Portfolio Snapshot

```bash
python3 <ASSISTANT_TOOLS_DIR>/finances/portfolio-snapshot.py [options]
```

| Flag | Purpose |
|------|---------|
| (none) | Save today's snapshot + show diff vs yesterday |
| `--diff-only` | Show diff without saving (fails if no snapshot today) |

Output includes: top gainers, top losers, account balance changes.

### Transaction Snapshot

```bash
python3 <ASSISTANT_TOOLS_DIR>/finances/transaction-snapshot.py [options]
```

| Flag | Purpose |
|------|---------|
| (none) | Save today's snapshot + show transactions |
| `--summary` | Show spend/income summary (fails if no snapshot today) |

Output includes: total spend, total income, individual transactions.

## Accounts

Populate with your own accounts — no real account data should live in the framework library. Example shape:

| Account | Type |
|---------|------|
| `<Bank> Checking x<NNNN>` | Checking |
| `<Bank> HYSA x<NNNN>` | Savings |
| `<Card Issuer> Card x<NNNN>` | Credit card |
| `<Brokerage> Individual x<NNNN>` | Brokerage |
| `<Brokerage> Roth IRA x<NNNN>` | Retirement |
| `<Crypto Brokerage> x<NNNN>` | Crypto |

**Known quirks (replace with your own once you've used the skill for a while):**
- Some brokerages report `$0` cost basis for all holdings — handle in aggregation.
- Use a 7-day (or longer) lookback to catch delayed postings from SimpleFIN.
- Watch for auto-payment transactions that look like purchases — e.g., a card-balance auto-pay from checking that shares the merchant name with actual purchases on that card. Document the pattern so the skill doesn't double-count.

## Automated Snapshots (Cron)

- Portfolio: 7:00 AM ET weekdays (`--save-only`)
- Transactions: 6:00 AM ET daily (`--save-only`)
- Cron log: `<ASSISTANT_TOOLS_DIR>/finances/cron.log`

These save snapshots so briefings can use `--diff-only` / `--summary`.

### Always show data freshness

When presenting financial data, always include when the snapshot was last updated:
```bash
ls -la <ASSISTANT_TOOLS_DIR>/finances/portfolio-snapshots/ | tail -1
ls -la <ASSISTANT_TOOLS_DIR>/finances/transaction-snapshots/ | tail -1
```

If the latest snapshot is **older than 24 hours**, flag it: "Data as of [date] — stale, refreshing now..." and run the script to get fresh data.

### Debugging sync failures

If snapshots are missing or stale, check:

1. **Cron log** for errors:
   ```bash
   tail -30 <ASSISTANT_TOOLS_DIR>/finances/cron.log
   ```

2. **Common failure: SimpleFIN timeout.** The API at beta-bridge.simplefin.org can be slow. Scripts retry 3x with 60s timeout. If all retries fail, the cron exits non-zero and no snapshot is saved.

3. **Test SimpleFIN connectivity:**
   ```bash
   : "${SIMPLEFIN_ACCESS_URL:?not in the session env - declare it with export in a .env tier, then restart the bot}"
   curl -s --max-time 30 "$SIMPLEFIN_ACCESS_URL/accounts?version=2" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d.get(\"accounts\",[]))} accounts')"
   ```

4. **Env var missing in cron:** Crons source `~/.env` via `. <USER_HOME>/.env`. If the env file is malformed or the var is missing, the script prints "ERROR: SIMPLEFIN_ACCESS_URL not set" to cron.log.

5. **Manual refresh:** If cron failed, run the scripts manually:
   ```bash
   : "${SIMPLEFIN_ACCESS_URL:?not in the session env - declare it with export in a .env tier, then restart the bot}"
   python3 <ASSISTANT_TOOLS_DIR>/finances/portfolio-snapshot.py
   python3 <ASSISTANT_TOOLS_DIR>/finances/transaction-snapshot.py
   ```

## Operations

### 1. Daily Summary (used by /briefing)

Run both scripts. If no snapshot exists today, run without flags first to save, then the output already includes the diff/summary.

```bash
: "${SIMPLEFIN_ACCESS_URL:?not in the session env - declare it with export in a .env tier, then restart the bot}"
python3 <ASSISTANT_TOOLS_DIR>/finances/portfolio-snapshot.py
```

```bash
: "${SIMPLEFIN_ACCESS_URL:?not in the session env - declare it with export in a .env tier, then restart the bot}"
python3 <ASSISTANT_TOOLS_DIR>/finances/transaction-snapshot.py
```

### 2. Portfolio Check

Show current balances and day-over-day changes:

```bash
: "${SIMPLEFIN_ACCESS_URL:?not in the session env - declare it with export in a .env tier, then restart the bot}"
python3 <ASSISTANT_TOOLS_DIR>/finances/portfolio-snapshot.py
```

### 3. Recent Transactions

Show today's transactions:

```bash
: "${SIMPLEFIN_ACCESS_URL:?not in the session env - declare it with export in a .env tier, then restart the bot}"
python3 <ASSISTANT_TOOLS_DIR>/finances/transaction-snapshot.py
```

### 4. Historical Snapshots

Read previous snapshots directly:

```bash
ls <ASSISTANT_TOOLS_DIR>/finances/portfolio-snapshots/
ls <ASSISTANT_TOOLS_DIR>/finances/transaction-snapshots/
```

Then read a specific date's snapshot with the Read tool.

## Output Formatting

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

Format results concisely for Telegram:

**Briefing format:**
```
FINANCE
Portfolio: <Brokerage A> $<balance> (+X%), <Brokerage B> $<balance> (-X%)
Cash: <Bank> checking $<balance>, HYSA $<balance>
Cards: <Card A> -$<balance>, <Card B> -$<balance>
Transactions: $<spent> (<merchant 1> $<amt>, <merchant 2> $<amt>)
```

**Flag unusual items:**
- Charges over $200
- Possible new subscriptions
- Unfamiliar merchants
- Large balance swings (>5% on investment accounts)

## Instructions

1. Run the `SIMPLEFIN_ACCESS_URL` check before the scripts. Never `source ~/.env`: the variable is already in the session (see Environment Setup)
2. If a snapshot doesn't exist for today, run without flags first (saves + shows)
3. If `--diff-only` or `--summary` fails, run the base command instead
4. On weekends/holidays, portfolio values may be unchanged — note this briefly
5. When called from /briefing, run portfolio and transaction scripts in parallel

$ARGUMENTS
