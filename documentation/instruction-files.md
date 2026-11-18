# Instruction files: how Claude Code and Codex load them

Reference moved from the root `CLAUDE.md` ("Instruction files: CLAUDE.md and AGENTS.md"), which keeps the rules; this file adds none.

Claude Code reads `CLAUDE.md`; Codex reads `AGENTS.md`.

In the paragraph below, "this file" means the root `CLAUDE.md`.

Every session started in this checkout loads this file whole, and so does a session started in a folder under it, since Claude Code reads each parent directory's `CLAUDE.md`. A composed bot is the exception: the composer lists the data root's `CLAUDE.md` under `claudeMdExcludes` in every bot's `.claude/settings.local.json` (`compose_settings_local` in `claudlobby/composer.py`), so a bot under a data root that is this checkout does not load this file, and its own `CLAUDE.md` still loads. Nested files load on use only. Claude Code reads one when a session opens a file in its folder, if that folder is under where the session started. A bot's own checkouts, under `projects/` in its directory, are such folders: a bot that opens a file in one loads that folder's `CLAUDE.md`.
