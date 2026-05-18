# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Phase 4 Part A — autonomous-runner schema layer.** New `autonomous_runner` block on `BotConfig`, parsed from `fleet.yaml` by `_coerce_bot`, validated in `validate()`, rendered into the bot's `CLAUDE.md` by `compose_claude_md`. Adds three dataclasses (`AutonomousRunnerConfig`, `AutonomousRunnerPicker`, `AutonomousRunnerBypass`) and 23 new tests across `test_config.py`, `test_validator.py`, `test_composer.py`. The wrapper-skill body, risk-classifier prompt, fleet.yaml example, and Autonomous Worker archetype docs live in Part B (issue #279). See [Claudfather/Claudlobby#278](https://github.com/Claudfather/Claudlobby/issues/278).
- **Phase 4 Part B — autonomous-runner skill body.** New `library/skills/autonomous-runner/` with `SKILL.md` (the 10-step wrapper procedure: idle check, quota, pick, classify, pre-hooks, invoke, parse, post-hooks, on-outcome, state update), `risk-classifier-prompt.md` (the `structural_vs_mechanical` subagent prompt template), and `archetype.md` (the "Autonomous Worker" composition recipe). `fleet.yaml.example` gains a commented `autonomous_runner` block and a complete `dbt-auto-bot` example bot stanza. clauDNA skills are invoked through the Skill tool as plugin-namespaced commands (e.g., `/claudna:implement-plan --auto`); no filesystem-path references. 12 markdown sanity tests in `test_autonomous_runner_assets.py`. End-to-end validation deployment is deferred to Phase 4 Part C. See [Claudfather/Claudlobby#279](https://github.com/Claudfather/Claudlobby/issues/279).

### Security

- Restrict .env file permissions to 0o600 (owner read/write only) in scaffold_env_files and env-migrate. Previously these files were created with default umask (typically 0o644), making fleet secrets world-readable on shared hosts.
