### Removed — four `supervisor.sh` verbs that nothing called (#1607)

`svc_state`, `svc_restart_host`, `svc_job_hosts_caller` and `svc_enroll_agent` are removed from `claudlobby/_runtime_scripts/supervisor.sh`, with their sections in `tests/test_supervisor_adapter.sh`: 210 lines of the adapter and 139 of its contract test, net. No script, unit, timer, skill or Python caller named any of them, and none is on the list of adapter functions the Python owner may call.

The verbs that have callers are unchanged: `svc_is_registered`, `svc_kick`, `svc_enroll`, `svc_disenroll`, `svc_unit_name` and every `svc_bot_*`, `svc_activation_*`, `svc_inventory_*`, `svc_host_job_*` and `svc_update_job_*` verb.

`svc_enroll`, `spin-up-bot.sh` and the two `install-bot` scripts stay for now. `reconcile-fleet.sh --enroll` repairs an orphan (a live session with no installed unit) through them, and `bot start` and `bot stop` cannot do that yet.
