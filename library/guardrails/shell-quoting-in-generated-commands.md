---
title: Text interpolated into a shell command is executed, not quoted
description: Backticks and $( ) inside double quotes are command substitution and !word is history expansion — any generated command carrying arbitrary text must use single quotes, a file, or stdin
---

# Text interpolated into a shell command is executed, not quoted

When you build a shell command string that carries text you did not write — a message body, an issue
comment, a dispatch payload, a handle — the shell interprets that text before the command runs.

Two constructs do it, and both are properties of the shell rather than of any tool:

- **Backticks and `$( )` inside double quotes are command substitution.** The shell *executes* what they
  enclose and substitutes the output.
- **`!word` in an interactive shell is history expansion.** It silently mangles or blanks the text around
  it, and can swallow the newline so the command is never submitted. Disable with `set +H;`.

## The rule

- **Single-quote** any command string carrying text you did not author. If double quotes are required,
  escape every backtick as `` \` ``.
- Better: **do not interpolate at all.** Pass the payload via a file, stdin, or an encoding flag
  (`--data-urlencode`), so the text never becomes part of a command string.
- Prefix `set +H;` on anything that may contain `!word`.

## This is a security property, not a formatting nicety

`lib/gh-mention-guard.sh` rewrites `@handle` out of GitHub-bound calls. It uses two *different*
replacements by surface, and the asymmetry is deliberate: MCP gets `` `handle` `` (safe in a JSON field),
Bash gets bare `handle` — **never backticks**. Its own comment records why:

> A comment body normally sits inside a double-quoted shell string, where a backtick is COMMAND
> SUBSTITUTION. Verified: the naive backtick rewrite makes the shell EXECUTE the handle, turning a
> notification bug into arbitrary code execution.

The obvious fix for one bug was arbitrary code execution, because the rewrite targeted a *string* that
was really a *command*.

**Related:** the `!word` rule's tmux application — and the fact that `lib/dispatch.sh` prepends `set +H;`
for you, so hand-rolled `send-keys` is the exposed path — is in the `dispatch` protocol and
`library/lessons/tmux-dispatch-shell-expansion.md`. For a pipeline hiding a command's exit status, a
separate mechanism, see the `exit-status-through-pipes` guardrail.
