"""Package-owned registries (design §9b census + §11 field policy).

Phase 1 ships FIELD_POLICY (the classification registry — the ENFORCEMENT
source of truth: contracts read caps from here, the capture door reads
CONTENT membership from here; editing a cap HERE changes behavior).
SYSTEM_EVENT_SEVERITY (the system-event vocabulary, each type with its
severity) and METRIC_NAMES joined in Phase 2b.
"""

from __future__ import annotations

#: Who the reporter was TO THE PR they are citing (#1666). A CLOSED vocabulary
#: rather than free text, and the closure is the point: the consumer of this
#: field decides whether a bot may merge, so an unrecognised value must be a
#: refusal at the door rather than a string nobody can classify later.
#:
#: **There is deliberately no "unknown" member.** Absent (None) IS the third
#: state, and it has to stay distinguishable from `reviewed`: 19% of in-epoch
#: PRs have no citing report at all, and rung 1 must REFUSE for those rather
#: than read "no role recorded" as "not an author" and pass. A member spelled
#: `unknown` would invite a writer to record one, which converts an absence the
#: consumer can refuse on into a value it might accept.
PR_ROLES = ("authored", "reviewed")

# (family, field) -> {class: CONTENT|SENSITIVE|DIAGNOSTIC|METADATA,
#                     cap: bytes, proof: keep sha/bytes triple on drop}
FIELD_POLICY: dict[tuple[str, str], dict] = {
    ("communication", "body"): {"class": "CONTENT", "cap": 16_384, "proof": True},
    ("communication", "recipient_raw"): {"class": "SENSITIVE"},
    ("work_item", "body"): {"class": "CONTENT", "cap": 16_384},
    ("task", "summary"): {"class": "CONTENT", "cap": 4_096},
    # chunk M-A (#1481) — the task loop's authored text: why a task was
    # withdrawn or nudged, and what an escalation asks. Same class and cap as
    # `summary`; a metadata-mode capture strips all three together.
    ("task", "reason"): {"class": "CONTENT", "cap": 4_096},
    ("task", "question"): {"class": "CONTENT", "cap": 4_096},
    # `by` is METADATA, deliberately: the card must still name WHO acted after
    # a metadata-mode capture strips the prose beside it ("needs you" with no
    # asker is a question nobody can route). But it is AUTHORED INPUT all the
    # same -- `--as <who>` and a bot name -- so it is capped like everything a
    # human types (the M-A fold, F11): unregistered, it was the one authored
    # field in the family with no cap at all, and a 4 KB "name" would ride into
    # every attention card. Small, because it is a NAME: the doors already
    # clamp an alias to 64 characters.
    ("task", "by"): {"class": "METADATA", "cap": 128},
    # `pr_role` is METADATA for the same reason as `by`, and registered
    # EXPLICITLY rather than left out (#1666): an unregistered field is not in
    # CONTENT_FIELDS and so survives a metadata capture by ACCIDENT. This one
    # must survive by RULE, because the failure is silent and passes -- a
    # stripped role reads as "no role recorded", which a merge gate reads as
    # "not the author". Stating the class here is what makes a future edit that
    # reclassified it a visible change rather than an omission.
    #
    # No cap, deliberately: it is a closed Literal (contracts.PR_ROLES), not
    # authored text, so the contract bounds it and a cap would never fire.
    # SENSITIVE entries below already carry no cap, so the shape is precedented.
    ("task", "pr_role"): {"class": "METADATA"},
    # Same class and the same reason as pr_role (#1710): provenance, not prose.
    # A metadata capture that stripped it would collapse "auto-resolved" into
    # "absent", and absent is a distinct third state meaning the writer predates
    # the field. Closed Literal, so no cap -- the contract bounds it.
    ("task", "link_source"): {"class": "METADATA"},
    # Same class, same reason again (#1711 B): a stripped withholding marker
    # reads as "nothing was withheld", which a consumer reads as "no attribution
    # was ever declared" -- the precise false clear the field exists to prevent,
    # re-entering through its own remedy. Closed Literal, so no cap.
    ("task", "pr_attribution_withheld"): {"class": "METADATA"},
    ("workstream_event", "note"): {"class": "CONTENT", "cap": 4_096},
    ("workstream_event", "waiting_on"): {"class": "METADATA"},
    ("workstream_event", "next_step"): {"class": "CONTENT", "cap": 4_096},
    ("transmission", "destination"): {"class": "SENSITIVE"},   # rides detail
    ("system", "data"): {"class": "DIAGNOSTIC", "cap": 16_384},
}

CONTENT_FIELDS: dict[str, tuple[str, ...]] = {}
for (_family, _field), _pol in FIELD_POLICY.items():
    if _pol["class"] == "CONTENT":
        CONTENT_FIELDS[_family] = CONTENT_FIELDS.get(_family, ()) + (_field,)


def cap_for(family: str, field: str) -> int:
    return FIELD_POLICY[(family, field)]["cap"]


# kind=system severity is REGISTRY-OWNED (§9b: "ingest stamps it from the
# package-owned seed module; callers cannot set it; unknown type => null").
# A caller-supplied severity is a caller bug (ContractViolation via the strict
# wire model).
#
# This dict is the fleet's one event-type vocabulary: every type the runtime,
# the library and the package record is a key (a harness records only into its
# own throwaway plane). An unknown token still INGESTS (F19), but with NULL
# severity, so no critical read can show it, and registering it later does not
# re-stamp the rows already stored. tests/test_event_type_registry.py fails on
# a writer whose type is missing here, and on a document whose own list
# disagrees with this one; tests/test_service_is_crash_looping.py fails on a
# fleet-pulse list that names a type not critical here.
#
# critical is what `event list --critical` and a bot's brief select, and what
# fleet-pulse's two reads choose their types from; notice is the record. The
# rule for a new type: raised through emit_failure_alert (a FLEET ALERT),
# critical; through emit_fleet_notice or notify_currency (a FLEET NOTICE),
# notice; recorded directly by a writer, notice unless it is a fault that writer
# pages someone to fix.
SYSTEM_EVENT_SEVERITY: dict[str, str] = {
    "fleet_alert": "critical",
    "fleet_notice": "notice",
    "daemon_started": "notice",
    "daemon_stopping": "notice",
    "spool_drain_completed": "notice",
    # cutover chunk 3 — the shadow primitive's record (J4). The shadow is
    # gone (F18 R2a); the names stay REGISTERED so the rows it recorded
    # still classify.
    "shadow_parity_clean": "notice",
    "shadow_parity_diverged": "critical",
    # cutover chunks 5 / 6b — the epochs the transition recorded (a reader
    # declared, the legacy writes retired). The machinery is gone (F18 R3:
    # the plane is the only source); the names stay REGISTERED so the rows
    # the estate recorded still classify.
    "cutover_declared": "notice",
    "legacy_write_retired": "notice",
    # cutover chunk 7a — a report whose status reached no task event (a terminal
    # note that resolved nothing): the status the idle-worker check reads.
    "report_status": "notice",
    # The faults the runtime records about a bot or its fleet: a session or
    # unit gone, a pane that stopped working, a failed script, an overdue
    # dispatch, a bridge down, a reload or restart that failed, a start that
    # never settled.
    "session_missing": "critical",
    "service_down": "critical",
    "activity_stuck": "critical",
    "script_error": "critical",
    "overdue_dispatch": "critical",
    "bridge_down": "critical",
    "reload_failed": "critical",
    "restart_failed": "critical",
    "rc_timeout": "critical",
    # #1769: a unit failing its start over and over, which the per-phase boot
    # gate had been reading as "boot in flight" forever. Critical so the
    # escalation read (severity = 'critical') can page it.
    "crash_loop": "critical",
    # #2070: a bot whose input box holds text that was never submitted, with no
    # turn running (keepalive's HELD verdict), paged in place of activity_stuck.
    # Critical like the page it replaces; the remedy is an operator Enter, not
    # a restart, so it is not one of fleet-pulse's Telegram escalation types.
    "input_held": "critical",
    # #1924: historical launchd reenrollment deferral, retained for old facts.
    "job_reenroll_deferred": "notice",
    "alert_delivery_failed": "notice",
    "dispatch_orphaned": "notice",
    # fleet-pulse pushes it to the manager, but as routing, not a fault.
    "worker_unassigned": "notice",
    "pane_stuck": "notice",
    "wip_uncommitted": "notice",
    "send_miss": "notice",
    "send_retry": "notice",
    "send_blind": "notice",
    "send_blind_recovered": "notice",
    # #1236: a send that was not submitted. The box never showed the typed
    # payload, so the Enter was withheld (payload-not-shown), or it still showed
    # it after the last Enter (payload-still-in-box). The text is in the box, or
    # may still land there, unsubmitted.
    "send_unsubmitted": "notice",
    # #2105: a CLI delivery the receiver had not submitted after the receipt wait,
    # whose box held exactly that message in an idle pane, got one or two Enters
    # from the messaging operation owner. data.match says whether the box showed
    # the message's text or only a paste chip.
    "delivery_enter_repaired": "notice",
    # #2036: a pane send that went out WITHOUT the recipient's send lock,
    # because no lock could be taken at all (a refused one is send_miss).
    "send_unlocked": "notice",
    "resume_skipped": "notice",
    "plugin_marketplace_failed": "notice",
    # #2158: a bot session start on Linux that did not run under its own child
    # subreaper, so the session's orphans (if it started) re-parent to the user
    # manager; data.report says why, or why the start failed.
    "bot_subreaper_unavailable": "notice",
    # #2184: the pulse found a live session's tmux server whose parent is not
    # a bot-subreaper (it died mid-session, or never took its name);
    # data.parent names what the server's parent is now.
    "bot_subreaper_missing": "notice",
    "briefing_deferred": "notice",
    "briefing_dispatched": "notice",
    "briefing_failed": "notice",
    # #1826: a slot that was missed, sent once as a FLEET NOTICE. notice -- the
    # notice path's own manager push and Telegram line are the page.
    "briefing_missed": "notice",
    # manager check-in (spec section 5): the beat fired, or it did not and why.
    # Ratelimit and unreachable are deliberately NOT rows -- the first is
    # derivable from checkin_triggered, the second cannot reach the plane.
    "checkin_triggered": "notice",
    "checkin_skipped": "notice",
    "audit_selected": "notice",
    "audit_dispatched": "notice",
    "audit_deferred": "notice",
    "audit_failed": "notice",
    "sweep_repo_unreachable": "notice",
    "bot_teardown_started": "notice",
    "handoff_skipped": "notice",
    "fleet_rescue": "notice",
    # cutover B2 — the keepalive tick's transitions and the vitals hook, through
    # the fleet-event door (the per-tick verdicts ride the heartbeat sample)
    "keepalive_restart": "notice",
    "bridge_heal": "notice",
    "keepalive_skip": "notice",
    "keepalive_reload": "notice",
    "tool_call": "notice",
    # chunk K (#1467): `claudlobby fleet reports ack` records the viewer's read
    # position as a plane fact — informational, never an alert
    "reports_acked": "notice",
    # #1503: the per-finished-session digest (transcript-digest.sh SessionEnd
    # hook), moved off the retired transcript-digest-<date>.jsonl onto the
    # plane. Informational — the monitor's substrate, never an alert.
    "session_digest": "notice",
    # manager check-in (spec §7): the decision record, and the join row a
    # `dispatch-task.sh --checkin` appends to its batch. notice — the record
    # IS the point; nothing here pages.
    "checkin_decision": "notice",
    "checkin_dispatch": "notice",
    # #1686: the host's heavy-job slot. A hold and its release, a call refused
    # because every slot was taken or a free one was another caller's turn, a
    # hold whose holder died without a release (across_reset: it was running
    # when the host reset, #1644's evidence), a heavy-looking command the
    # matcher would not parse, and (#2124) a queue ticket dropped because its
    # holder stayed silent once a slot was free. The record, never a page.
    "heavy_slot_acquired": "notice",
    "heavy_slot_released": "notice",
    "heavy_slot_refused": "notice",
    "heavy_slot_unreleased": "notice",
    "heavy_slot_unparsed": "notice",
    "heavy_slot_ticket_dropped": "notice",
    # The public-write guard: a refused GitHub-bound write (the record), and an
    # armed guard that found no host list. That one is critical because the
    # protection someone armed is off until the list is written: it shows in
    # `claudlobby event list --critical` and under ALERTS in the bot's brief. It
    # does not page (fleet-pulse pages a fixed list of types).
    "public_write_refused": "notice",
    "public_write_guard_unarmed": "critical",
    # #2090: the credential-echo guard. A Bash call refused because it would
    # print an env-held credential (the row and the CLI, never the command),
    # and a command its decider could not read (allowed, and counted). The
    # record, never a page.
    "credential_echo_refused": "notice",
    "credential_echo_unparsed": "notice",
    # #1069: the signal guard. A Bash call refused because it would signal a
    # process the caller did not start (the kinds of target, never the
    # command). The record, never a page.
    "signal_guard_refused": "notice",
    # FLEET ALERTs (emit_failure_alert): each records its caller's own type and
    # pages the manager and Telegram. Where each row lands is in the
    # fleet-observability protocol's table. From the host jobs: disk-monitor,
    # fleet-memory-check, host-health-check (one of three types, by finding),
    # update-claude-code and vault-sync.
    "disk_high": "critical",
    "memory_high": "critical",
    "undervoltage": "critical",
    "storage_stall": "critical",
    "host_health": "critical",
    "binary_update_failed": "critical",
    "binary_unrunnable": "critical",
    "vault_sync_failed": "critical",
    # ...and from the fleet jobs: a bot's keepalive that failed to run
    # (keepalive-all), a halted rolling restart, and the alert target checks of
    # fleet-pulse and creds-check.
    "keepalive_failed": "critical",
    "rolling_restart_stalled": "critical",
    "alert_target_refused": "critical",
    "alert_pair_unreachable": "critical",
    # FLEET NOTICEs (emit_fleet_notice): the same channels, framed as routine.
    "orphan_browser_reaped": "notice",
    "binary_update_skipped": "notice",
    "binary_prune_skipped": "notice",
    "binary_repaired": "notice",
    "vault_sync_recovered": "notice",
    # notify_currency: the debounced source-currency notices of notify-behind
    # and update-siblings.
    "source_behind": "notice",
    "source_release_gap": "notice",
    "sibling_update_blocked": "notice",
    "sibling_update_failed": "notice",
    "sibling_updated": "notice",
    # Records nobody is paged for: which manager a host-scoped signal reached
    # (lib-common), a failed App token mint (git-credential-github-app), each
    # declared bot at a host boot and the boot's summary (boot-capture), the
    # vault git-state guard's denials and the targets it could not read,
    # vault-sync's failed-sync row on the vault itself (its page is
    # vault_sync_failed), and the code-audit-sweep skill's completion, which
    # the agent records.
    "alert_recipient_resolved": "notice",
    "auth_mint_failed": "notice",
    "boot_capture": "notice",
    "boot_capture_summary": "notice",
    "vault_guard_denied": "notice",
    "vault_guard_unresolved": "notice",
    "vault_sync": "notice",
    "audit_completed": "notice",
    # The Python writers' records: a local operator's first contact
    # (operation_context), a task re-check and an empty one (task_recheck), and
    # an empty workstream prune (workstream_operations).
    "operator_first_seen": "notice",
    "task_recheck": "notice",
    "task_recheck_noop": "notice",
    "workstream_prune_noop": "notice",
}

# ---------------------------------------------------------------------------
# Phase 2b: the metric-name registry (§9b MetricSample — open registry,
# warn-on-unknown at ingest, additions by PR). Units live HERE, never on
# rows. Seed = the spec's walked list (§9b Emitters paragraph).
# ---------------------------------------------------------------------------

METRIC_NAMES: dict[str, dict] = {
    "host.load": {"unit": "load", "description": "1/5/15-min load triplet"},
    "host.mem_available_mb": {"unit": "MB", "description": "available RAM"},
    "host.disk_free_gb": {"unit": "GB", "description": "free disk"},
    "host.thermal_flags": {"unit": "flags", "description": "Pi vcgencmd thermal"},
    "host.undervoltage": {"unit": "bool", "description": "Pi undervoltage flag"},
    "host.boot_time": {"unit": "iso8601", "description": "last boot instant"},
    "host.job_ran": {"unit": "run", "description": "one sample per machinery run"},
    "host.plane_wal_bytes": {"unit": "B",
                             "description": "size of the plane's WAL; over"
                                            " 4 MiB means a reader is holding"
                                            " a snapshot (#1905)"},
    # #1644: what splits load into CPU and IO (Linux /proc; absent elsewhere)
    "host.swap_used_mb": {"unit": "MB", "description": "swap in use"},
    "host.swap_pages": {"unit": "pages",
                        "description": "pages swapped in and out since boot"
                                       " ({in, out}; a rate is the difference"
                                       " of two samples)"},
    "host.procs": {"unit": "procs",
                   "description": "processes runnable and blocked on IO now"
                                  " ({running, blocked})"},
    "host.cpu_ticks": {"unit": "ticks",
                       "description": "iowait and total CPU ticks since boot"
                                      " ({iowait, total}; the iowait share is"
                                      " the ratio of their differences)"},
    "vault.behind": {"unit": "commits", "description": "behind upstream"},
    "vault.ahead": {"unit": "commits", "description": "ahead of upstream"},
    "vault.last_fetch_age_s": {"unit": "s", "description": "age of last fetch"},
    "vault.fetch_failed": {"unit": "bool",
                           "description": "a failed fetch must never render"
                                          " as up-to-date"},
    "bot.session_up": {"unit": "bool", "description": "tmux session alive"},
    "bot.bridge_up": {"unit": "bool", "description": "telegram poller alive"},
    "bot.rc_ok": {"unit": "bool", "description": "remote control live"},
    "bot.pane_last_change_age_s": {"unit": "s", "description": "pane activity age"},
    "bot.heartbeat": {"unit": "run", "description": "keepalive heartbeat"},
    "bot.rss_mb": {"unit": "MB", "description": "resident set size"},
    "env.key_state": {"unit": "state",
                      "description": "creds-check key state (names never"
                                     " values; the #1213 present-but-empty"
                                     " class)"},
}
