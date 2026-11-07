# Instruction files: how Claude Code and Codex load them

Reference moved from the root `CLAUDE.md` ("Instruction files: CLAUDE.md and AGENTS.md"), which keeps the rules; this file adds none.

Claude Code reads `CLAUDE.md`; Codex reads `AGENTS.md`.

In the paragraph below, "this file" means the root `CLAUDE.md`.

Every session started in this checkout loads this file whole, and so does every bot under it, since Claude Code reads each parent directory's `CLAUDE.md`. Nested files load on use only. Claude Code reads one when a session opens a file in its folder, if that folder is under where the session started (so bots never do).
