# Self-addressed report rows (#1754)

Before #1754 a manager's `report-back.sh` delivered into the manager's own tmux
pane, and the plane recorded the communication with `recipient_alias ==
sender_alias`, and landed the task event it carried (a terminal one closed the
task; a progress one did not). Those rows exist on every fleet
that ever had a manager report upward: measured on the two fleets of the
authoring host at the time of the fix (12 rows, two managers), and reported
by an external reviewer on two more planes.

## Ruling: history stays

The plane is append-only and the rows are true: the door did deliver to the
sender. They are not rewritten, and the tasks a terminal one closed are not reopened —
the work they reported is past, and reopening would page every manager on the
estate about rows nobody can act on. New rows cannot have this shape: the door
refuses a self-addressed send (rc 4) before it records anything.

## Finder

Read-only. Open the db with a plain connection held by `PRAGMA query_only`
(a `mode=ro` URI cannot create the WAL shared-memory file under the system
`python3` when the writer has closed — `plane-readers.connect` has the same
fallback):

```bash
python3 - <<'EOF'
import sqlite3
c = sqlite3.connect("state/plane/plane.db"); c.execute("PRAGMA query_only=1")
q = """
SELECT sender_alias, COUNT(*), MIN(occurred_at), MAX(occurred_at)
  FROM communications
 WHERE emitter = 'report-back' AND message_class = 'report'
   AND sender_alias = recipient_alias
 GROUP BY 1"""
for row in c.execute(q):
    print(row)
EOF
```

Per row, with the task event it carried (if any) — `e.event` says whether it
was terminal (`completed`, `failed`, `blocked`) or a `progress` update, which
closed nothing:

```sql
SELECT c.occurred_at, c.sender_alias, c.msg_id, e.event, a.source_ref
  FROM communications c
  LEFT JOIN events e ON e.source_ref = c.source_ref AND e.kind = 'task'
  LEFT JOIN assignments a ON a.assignment_id = e.assignment_id
 WHERE c.emitter = 'report-back' AND c.message_class = 'report'
   AND c.sender_alias = c.recipient_alias
 ORDER BY c.ingest_seq;
```

What a hit means: the report's prose reached nobody but its author. If the
answer mattered and never reached the human by another carrier (Telegram, a PR
comment), it is the human's call whether to ask again; the plane row is not the
place to fix that.
