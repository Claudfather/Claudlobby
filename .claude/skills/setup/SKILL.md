---
name: setup
description: "Prepare one managed Git installation and an explicitly approved first fleet."
argument-hint: "[--check-only]"
---

# Setup

Follow [Getting started](../../../documentation/getting-started.md) for the single installation path and [First-team setup](../../../documentation/first-team-setup.md) for the optional lead/engineer/reviewer template. Browser newcomer cold-start acceptance is not established; do not promise a working browser launch or substitute historical sealed-release commands.

For `--check-only`, inspect prerequisites without changes: an explicit independent CPython >=3.10 that can create and run a venv with ssl, Git, tmux, jq, Claude Code, and the native user manager. Follow the [Python executable check](../../../documentation/getting-started.md#prove-the-python-executable-once). Linux unattended operation needs administrator-configured user lingering. Report missing prerequisites separately from provider authentication. Do not install system packages, change user permissions or enable lingering implicitly.

Obtain explicit trusted source, remote/ref, independent installation/data paths and interpreter from the operator. Do not search protected directories, guess a latest release or download, reset user files, or build/install an editable package. From the trusted source, run the documented stdlib `python -m claudlobby.bootstrap` command. Use its stable CLI for `host setup` with the same explicit inputs; host setup checks native prerequisites and reuses preparation, not a second installer. Preparation is distinct from activation and grants no owner access.

Stop at the credential gate before preparing agents: verify provider sign-in and intended repository/tool access, never borrow credentials or put secrets in arguments. No Telegram is required for teams without channels. `fleet.yaml.seed` is only an explicitly chosen operator Telegram path; complete its placeholders separately.

Use one `fleet setup --config FILE` or `--template revenue-web-app` command with the exact observed `--install-directory` and `--plan-only`. Review its retained request, source revision, configuration fingerprint and native scope. Stop before agent starts/provider spending until the operator approves that exact scope. Resume only with the same stable CLI's `host update continue`; inspect `host update status` after interruption. Never replace an unknown request or resend uncertain work.

Report blocked, prepared, committed and observed healthy states separately. Do not claim browser owner grants, public availability, useful work or restart durability from preparation. Preserve original receipts and keep merges/deployments under their separate authority.
