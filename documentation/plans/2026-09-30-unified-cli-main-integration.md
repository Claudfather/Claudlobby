# Main integration into the #1747 aggregate

The landing vehicle is **#1989**. This is a merge, not a rebase: aggregate parent
`b71532cbf412dbb1124dbddf72cbff2cc9ec11ea`, main parent
`c2edc54b399c2c8880057785a87366dd7a49e307`, merge base
`7c1e0e15bd85afe89f76ce87dcdad255e38b4dfc`. At this snapshot main contributes
18 commits and the trial merge conflicts in 26 paths. This includes #2024 and
#2013, which landed after the team's report at `49bbbccb`.

## Behavior ports and proof

Test names below identify the checks for each port; final run results are recorded
in the validation section. They are code/fixture evidence, not production proof.

| Upstream PR | Owner in the aggregate | Existing proof or adapted check |
|---|---|---|
| #1995 vault-sync explicit `--vault` | `lib/vault-sync.sh` | `tests/test_vault_sync.py::TestTheVaultIsNamedNotDiscovered` |
| #2001 Claudron doctor pending migrations, D007/D008 | `claudlobby/doctor.py` | `tests/test_doctor.py::TestClaudronDoctorRung`; diagnostic only, no `--fix` |
| #1975 digest isolated model invocation | `lib/transcript-digest.sh` | `tests/test_transcript_digest.sh`; optional authenticated `test_transcript_digest_isolation.py` remains separately gated |
| #1984 control-note resolver guard, deliberately unlinked report | `lib/plane-readers.py::answering_control_note`; explicit `task report` vs `fleet reports submit` | `tests/test_resolver_control_note_guard.py`; `test_message_write_cli.py::test_unlinked_report_routes_to_own_manager_without_task_effect`, now with a real open assignment |
| #1987 leading `set +H; ` receiver explanation | `templates/claude.md.j2`, dispatch/worker protocols and existing lesson | `tests/test_framed_dispatch_guidance.py` |
| #2014 empty briefing-unit listing must not trigger ERR | retired shell substitution removed; staged briefing unit set and activation/native inventory own replacement | `tests/test_briefing.py`, `test_fleet_job_reconcile.py`, `test_setup_backbone.py`; no `lib/setup-fleet` restoration |
| #1992 metric series reader | `claudlobby/plane/samples.py`, `commands/plane.py`, parser/identity/query helpers | `tests/test_plane_samples.py`: bounds, offsets, subject refusal, read-only and close-before-render |
| #2008 bridge readiness for the actual session | `lib/start-bot.sh` current pane PID probe; setup walkthrough requires current-session evidence | Existing startup/readiness coverage retained; this merge performs no authenticated boot |
| #2016 manager `/status` default | `defaults.py`, `composer.resolve_effective_skills`, `library/skills/status` | `tests/test_manager_status_default.py`, `test_status_skill.py`, `test_defaults_registry.py`, `test_seed_fleet.py`; fixture uses the single implicit manager |
| #2017 person-assigned standing goals and invalid assigner-pane route | `task_recheck.py` selection and `commands/task_recheck.py`; all due work goes to the current fleet manager | `test_task_recheck.py::test_a_persons_deadline_less_assignment_is_a_standing_goal`; preview and committed send exclude the goal and route due work to the manager |
| #2012 machine effects and safety documentation | `README.md` rewritten for sealed setup, with same-uid/process safety limits retained | `tests/test_cold_start_contract.py`; source inspection. The actual upstream commit did not contain `SECURITY.md`, despite its title |
| #2015 opt-in heavy-job slot | `composer._with_heavy_slot_hook`, `lib/heavy-slot-guard.sh`, `lib/heavy-slot.py`; config/switch registry | `tests/test_heavy_slot_{compose,guard,match,run}.py`; sealed native code and host data-root split pinned in guard tests |
| #2005 isolated coldstart harness, root fence, honest status | renamed `harness/coldstart-harness.sh` | `tests/test_coldstart_harness.py` |
| #2004 installed-tree proof and fake ID | outside-checkout bootstrap wheel proof in setup/getting-started, sealed resource gate | `tests/test_cold_start_contract.py`; installed direct/sdist wheel check in `test_package_resources.py` |
| #2007 stop at failed validation, honest install timing | validated `fleet setup` before activation; walkthrough uses `|| exit`; historical source-install timings labeled as such | `tests/test_cold_start_contract.py`; current staging refusal checks in `test_config_staging.py` |
| #2006 atomic private access.json and per-bot warning | `composer._write_atomic` and guarded compose path; existing staged config installer | `tests/test_composer.py` access-write cases, `test_config_staging.py`, `test_config_install.py` |
| #2024 heavy-slot pip/uv, flock/xargs, collect-only | private native `lib/heavy-slot.py` classifier | `tests/test_heavy_slot_2023.py` plus match/guard/run tests |
| #2013 Apache-2.0 | `LICENSE`, `NOTICE`, `pyproject.toml`; build/dev setuptools floor 77 | `tests/test_package_resources.py` direct and sdist wheel build |

### Deliberate mappings across the deleted doors

- The old `commands/task.py` sent reminders to the assigner. The aggregate already
  owns tasks at fleet level and sends one digest to the current manager. It never
  resolves a person's alias as a tmux recipient. Consequently this merge does
  **not** add the old per-person Telegram recheck route: the due row is the
  manager's responsibility. A person's assignment with no deadline is preserved
  as a disclosed standing goal; a passed deadline still makes it due. Unknown
  identity is not classified as a person. This follows the settled one-manager
  contract, rather than retaining two dispatch implementations.
- `report-back.sh --no-task` is represented by `fleet reports submit`. `task report`
  requires an explicit canonical task/assignment. An unlinked completion cannot
  close a current assignment. The surviving private resolver also retains main's
  unanswered-control-note guard for historical readers.
- `lib/setup-fleet` and its pattern-listing substitution no longer exist. Candidate
  configuration and unit inventory are handled by the staged activation owner.
  No deleted setup/report/task entrypoint is revived as a compatibility shim.
- `plane samples --json` joins the schema-1 envelope. Bad arguments exit 2;
  unavailable storage/no recorded subject exits 6. Pending migration/downgrade
  retain their specific public errors. Main's former bare JSON/exit-3 wrapper is
  replaced by the aggregate command-result contract.
- The newly defaulted status skill grants own-context brief/inbox reads and
  `gh pr list`, not a wildcard public CLI or arbitrary `gh` commands.

## Validation

Before-port source was exactly `b71532cb`, exported with its own installed test
resources, private HOME/TMPDIR and no live fleet settings.

- Heavy-slot composition failed on that source: an opted-in bot had no Bash hook.
- The ported standing-goal regression failed on that source: the deadline-less
  goal incorrectly appeared in the due-task list.
- The unmodified upstream status tests initially failed for invalid two-manager
  and obsolete `Paths` fixtures. Those failures are **not** red behavior proof;
  the fixtures were adapted to the aggregate contract before validation.

Combined validation in a private Python 3.12 export:

- The affected selection passed **1,234 cases**, skipped the one authenticated
  digest integration case, and found seven failures plus 14 setup errors. Those
  errors came from new upstream fixtures missing explicit Plane initialization,
  old `Paths` construction, manager-grant expectations, and the strengthened
  unlinked-report test initially checking the wrong event family.
- After fixture repairs, the four affected modules passed **348 cases** with one
  remaining seed-role expectation. Correcting that expectation to the seed's
  actual implicit manager passed **all 21 seed tests**. These counts overlap;
  they are not additive. No failure was deselected or production check weakened.
- The existing digest shell suite passed **70/70 checks**, including no MCP,
  plugin or hook load in its stub model invocation. The authenticated real-model
  test remains unrun; this is not a claim about native Claude startup.
- The successful selection includes direct and sdist wheel installation/resource
  equality, sample time offsets and read-only behavior, recheck/unlinked-report
  regressions, opt-in hook composition/classification, data-root isolation,
  access-file writes, coldstart fencing, setup, defaults and doctor diagnostics.

Hosted current-head CI is required independently of these local results. No test was run in the live checkout. No host pull,
fleet generation, service operation, bot restart or external notification occurred.
The imported `naked-bot-2026-09-30.json` is an earlier observation from main; it is
preserved as historical evidence and does not certify this candidate's grants.

## Host conversion and release hold

Use the [existing-host conversion runbook](../existing-host-release-conversion.md).
The old source checkout remains pinned while a separate build assembles the sealed
candidate. Stop/disable the exact old source-puller before it can cross this
boundary, preserve all recovery inputs, prove an independent same-OS canary,
then use `host activate --adopt-existing` in an authorized host-wide window.
Replace/enroll units and regenerate sessions through that one owner. Verify
all new sessions and timer targets before releasing the source hold. Never
re-enable the retired pull-root job.

This is **not** a per-bot rolling production restart. A protected busy bot blocks
its host's conversion window. Lumbergh remains protected until the operator
confirms his work is finished. Real user-systemd/Pi populated-host adoption,
interrupted-native recovery and normal-load timing remain separate acceptance
work; this merge supplies no new live proof. A code merge is not a deployment.

## Stack disposition and final freshness

`cb7924890092e72adfb096a28a5ee393db5f8bd4` (#1980) and
`1c156610995140bb2cf1440a703888e1956df000` (#1985) are ancestors of the aggregate
parent. **Do not merge #1985 separately.** #1989 is the landing vehicle based on
main; the #1747 implementation drafts from #1928 through #1985 are historical
review slices, not separate branches to keep rebasing or merging main into.
Keep their reviewed source stable. After aggregate acceptance/merge, verify each
slice has no unique unincorporated work before closing it as superseded. This
integration closes or merges no GitHub PR. #1925 remains the plan reference.

At the integration snapshot #2024 and #2013 are included; #2003 and #2021 are
still open and not included. Refresh main and PR heads immediately before any
landing decision. Current-head CI, disposition of substantive external findings,
and review of this amended revision are still required. The earlier green
#1985/#1989 revisions and old reviews do not certify this merge.
