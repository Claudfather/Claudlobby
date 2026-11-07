# Key commands

The command list moved from the root `CLAUDE.md` ("Key Commands"), which keeps the reboot snapshot and the test setup. Each command's `--help` is the reference.

```bash
# Composition
claudlobby config validate  # check fleet.yaml against library
claudlobby config diff [<FINGERPRINT>]  # current rendered drift, or the pending render
claudlobby host update apply [FULL_COMMIT]  # fetch the trusted source and apply one host update
claudlobby host update status  # original operation and consumer progress
claudlobby host update continue  # continue the same request
claudlobby library list  # available building blocks
claudlobby bot create --interactive  # author a bot; this does not start it

# Operations
claudlobby bot start <bot>  # start a declared bot through the lifecycle owner
claudlobby fleet start  # enroll and start the selected fleet's bots
claudlobby fleet reconcile  # audit the fleet's supervision state
claudlobby fleet status  # fleet health dashboard
claudlobby bot status <name>  # one bot in detail
claudlobby host doctor [--switches]  # pre-flight diagnostic; --switches: every switch and its arming line
claudlobby host credentials reconcile  # declared vs stored vs equipped credentials
claudlobby config validate --runtime  # self-containment audit (--strict, --bot)
claudlobby host supervision reap-orphans --dry-run  # stale supervision units; --apply removes
claudlobby fleet reports list [--since <RFC3339>]  # the fleet's reports, from the plane
claudlobby fleet reports ack --through <ACK_CURSOR> --request-id <UUID>  # explicit report acknowledgement
claudlobby fleet uptime  # per-bot uptime, MTBR, restart rate
claudlobby event list  # the fleet's events (rc 3 when the plane cannot answer)
claudlobby workstream list  # the fleet's workstream registry
claudlobby brief --bot <name> [--json] # one read door over fleet state for a bot
claudlobby host cache warm  # pre-download npx + uvx MCP packages
claudlobby bot move <bot> --to <fleet> # move a bot between fleets

# A manager's acts on one open task (#1481), and the check-in
claudlobby --json task withdraw TASK_ID --reason "…" --request-id UUID  # terminal withdrawal
claudlobby --json task escalate TASK_ID --question "…" --request-id UUID  # non-terminal: ask a human
claudlobby --json task nudge TASK_ID --reason TEXT --request-id UUID  # record a nudge and notify the fleet manager
claudlobby --fleet <F> task recheck --request-id UUID [--dry-run]  # ask the manager about due work
claudlobby --json --fleet F checkin <list|show> [...]  # check-in decisions and outcomes
claudlobby --json --fleet F checkin record --file FILE [--selection-file FILE] --request-id UUID [--dry-run]  # commit before acting
claudlobby checkin selection <verify|focus-refs> FILE  # offline selection checks

# Scaffolding and one-time migrations (see each --help)
claudlobby library create --kind <skill|guardrail>
claudlobby migration <env|data|cron|memory|lessons|workstreams>
```
