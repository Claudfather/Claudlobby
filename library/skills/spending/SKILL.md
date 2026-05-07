---
name: spending
description: "Use when the user asks about spending trends, subscription tracking, weekly/monthly spend, or category analysis. Deeper than /finance daily summary."
argument-hint: "[week|month|subscriptions|category] [options]"
---

# Spending

Analyze spending patterns, track subscriptions, and surface trends from transaction snapshots.

## Data Source

Transaction snapshots saved daily at `<ASSISTANT_TOOLS_DIR>/finances/transaction-snapshots/YYYY-MM-DD.json`
Portfolio snapshots saved daily at `<ASSISTANT_TOOLS_DIR>/finances/portfolio-snapshots/YYYY-MM-DD.json`

Requires `SIMPLEFIN_ACCESS_URL` env var:
```bash
source ~/.env
```

## Operations

### 1. Weekly Spend

Read the last 7 days of transaction snapshots and aggregate:

```bash
source ~/.env
for f in $(ls <ASSISTANT_TOOLS_DIR>/finances/transaction-snapshots/ | tail -7); do echo "=== $f ===" && python3 -c "
import json
data = json.load(open('finances/transaction-snapshots/$f'))
for acct in data.get('accounts', []):
    for tx in acct.get('transactions', []):
        print(f\"  {tx.get('payee','?'):40s} \${abs(tx.get('amount',0)):>10.2f}\")
"; done
```

Summarize: total spend, top merchants, average daily spend.

### 2. Monthly Spend

Same as weekly but for last 30 days of snapshots. Group by week.

### 3. Subscription Detection

Scan last 60-90 days of transactions for recurring charges:
- Same merchant, similar amount, monthly cadence
- Flag: subscriptions, annual fees, insurance, gym, etc.

Present as:
```
SUBSCRIPTIONS (detected)
- <Service A>: $<amt>/mo (last: <date>)
- <Service B>: $<amt>/mo (last: <date>)
- <Card>: $<annual fee>/yr (last: <date>)
Total recurring: ~$XXX/mo
```

### 4. Category Analysis

Group transactions by merchant type (best effort from merchant names):
- Food & Dining
- Transportation (Uber, gas)
- Shopping (Amazon, retail)
- Subscriptions
- Bills & Utilities
- Other

### 5. Unusual Charges

Flag transactions that are:
- Over $200
- From unfamiliar merchants (not seen in last 90 days)
- Significantly different from typical amount at same merchant
- Potential duplicate charges

### 6. Account Trend

Compare portfolio snapshots over time:
```bash
ls <ASSISTANT_TOOLS_DIR>/finances/portfolio-snapshots/ | tail -30
```

Read multiple snapshots and show balance trends per account.

## Output Formatting

When sending results via Telegram, use `format: "markdownv2"`. See [_telegram-formatting.md](../_telegram-formatting.md) for formatting rules.

The summary header must always include:
1. **Date range** with explicit start and end dates
2. **Data source note** — "from X snapshots" or "7-day API lookback as of [timestamp]"
3. **All three totals** — gross spend, refunds, income, and net (income - spend + refunds)

```
SPENDING — Mar 30 \- Apr 5 \(7\-day lookback, as of 4:00 PM\)

Gross spend: \$2,134 \| Refunds: \$1,351
Net spend: \$783 \(\$112/day\)
Income: \$8,483
Net: \+\$7,700

By category:
\.\.\. \(spend breakdown\)

Refunds:
\.\.\. \(refund items\)

Income:
\.\.\. \(income items\)
```

## Instructions

1. Always `source ~/.env` before running any finance commands
2. Read snapshot JSON files directly with the Read tool for analysis
3. For subscription detection, need at least 60 days of data
4. When comparing periods, note if snapshots are missing for some days
5. Round dollar amounts to 2 decimal places
6. Default to "this week" if no period specified
7. **Separate income from refunds.** Positive transactions are not all the same. Use merchant name and context to distinguish actual income (payroll, dividends, Zelle from people) from refunds/credits (returned charges, Amex statement credits, merchant refunds). Present them as separate sections — a $299 refund is not income, it's a return of spend. Show both the original charge and the refund transparently so the user can see what happened, rather than netting them into a single number.

$ARGUMENTS
