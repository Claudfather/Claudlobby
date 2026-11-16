### Fixed — a refused `host update runtime` or `siblings` names what stopped it (#2945)

The refusal used to read "selected host update scope cannot be verified" and nothing more, and a scheduled run's "has no verified committed job" dropped the failure it wrapped. Both now end with the failure's first line, cut to 500 characters, as `bot start` and the fleet lifecycle commands name theirs, and the JSON result carries it as `data.cause`. A failed `.env` tier resolver is named by its kind only, with a hint to `claudlobby host env tiers`, which shows the resolver's own text. When generated units differ from the committed configuration, `data.drift` lists every one of them.

### Fixed — the updater and its stages print a refusal's first line only (#2945)

`host update apply`, `continue` and `status` printed a refusal whole. A stage printed a traceback, so for an inventory refusal that ended with a native tool's stderr, the last line of that stderr was what `host update status` and the job's notice read as the cause. The updater now prints `claudlobby update:` and the refusal's first line, cut to 1,000 characters. A stage prints the same first line and exits 6. Any other failure in a stage keeps its traceback.

### Fixed — a refused update names every generated unit that differs from the committed configuration (#2945)

The check stopped at the first unit and named only its path, so a host learned one unit per run. It now reads every unit first, then names each one that differs, as missing or as changed, and says what changed (bytes, mode, a symbolic link, a non-regular file or a redirected directory). During `host update apply`, it also says whether the candidate configuration keeps or drops each unit. The refusal is still a `PlanError`, and which units pass or refuse is unchanged.

### Fixed — refusing a foreign launchd job now says which anchor it found and what to do (#2945)

A desktop app opened from a bot's shell inherits the bot's environment, and the inventory refuses such a job because its print mentions the installation's root. The refusal said only that. It now names the job's label and the anchor the print mentions (the data root, a unit's working directory, the native directory or the CLI). It also names the print fields where the anchor appears, from a fixed list, and quotes nothing else from the print. Then it gives the next step. When the anchor appears only in an environment block, the next step is to quit the app and start it from a shell that did not come from a bot pane. Elsewhere, it is to unload a leftover job with `launchctl bootout`. Which jobs are refused is unchanged.

### Fixed — operator hints name `host update apply`, not the retired activation (#2945)

The doctor's manifest provenance warnings, the `config validate` warning for an unarmed check-in, the ignition arm line for briefing slots, the guidance a new bot declaration prints and the install scripts' missing-unit refusals told the operator to stage a `config plan` and run `host activate`, a command that no longer exists. They now name `claudlobby --root <data-root> host update apply FULL_COMMIT_SHA`.

### Fixed — the operator check names what stopped it (#2945)

`host update apply` and `continue` first check that they run outside any managed unit, and `host repos`, `host channels`, `host backup` and `host owner` run the same check. It reads the generated units and the native inventory, and when either refused, it said only "operator ancestry could not be verified". So the units the drift check names, and the foreign job the inventory names, never reached the operator. The refusal now ends with the failure's first line, cut to 500 characters, as `bot start` and the fleet lifecycle commands name theirs, and the JSON result carries it as `data.cause`. Its kind is unchanged, and so is what it refuses.
