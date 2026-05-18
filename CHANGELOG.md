# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Phase 4 Part A — autonomous-runner schema layer.** New `autonomous_runner` block on `BotConfig`, parsed from `fleet.yaml` by `_coerce_bot`, validated in `validate()`, rendered into the bot's `CLAUDE.md` by `compose_claude_md`. Adds three dataclasses (`AutonomousRunnerConfig`, `AutonomousRunnerPicker`, `AutonomousRunnerBypass`) and 23 new tests across `test_config.py`, `test_validator.py`, `test_composer.py`. The wrapper-skill body, risk-classifier prompt, fleet.yaml example, and Autonomous Worker archetype docs live in Part B (issue #279). See [Claudfather/Claudlobby#278](https://github.com/Claudfather/Claudlobby/issues/278).

### Security

- Restrict .env file permissions to 0o600 (owner read/write only) in scaffold_env_files and env-migrate. Previously these files were created with default umask (typically 0o644), making fleet secrets world-readable on shared hosts.
