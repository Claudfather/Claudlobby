# Install patterns

Claudlobby installs as **one managed Git installation per host**: `INSTALL/src` (the Git checkout), `INSTALL/env` (its Python environment) and a stable `INSTALL/bin/claudlobby` entry point. The host data root is a separate, dedicated absolute path and is never inside the installation. The [cold-host walkthrough](getting-started.md) prepares that installation with `claudlobby.bootstrap` and `host setup`, then prepares the first fleet with `fleet setup`; later changes use `host update status` and `host update apply`, as described in the [update guide](fleet-update-lifecycle.md#managed-git-delivery). The walkthrough owns the commands; this page does not repeat them.

`host setup` checks `tmux`, Claude Code, and the native user manager; install and authenticate those prerequisites first. For a Telegram fleet, install its channel plugin and configure its token as the walkthrough describes. Tools used by optional jobs, such as `gh`, `jq`, Node, or Claudron, remain prerequisites of those jobs rather than implicit host-setup installs.

| Manager | Install directory | Persistence |
|---|---|---|
| macOS launchd | `$HOME/Library/LaunchAgents` | The current user domain owns the LaunchAgents. |
| Linux systemd user | `$HOME/.config/systemd/user` | Enable user lingering through the host administrator if jobs must survive logout. |

Use the exact install directory reported by `host setup`; do not guess a replacement if the manager is unavailable. Direct unit installers are private native primitives, not an alternative deployment path.

## Existing hosts

A host that was installed through the earlier sealed-release model, or that still pulls source into a live checkout, cannot move onto this installation by running the walkthrough over it. Its conversion needs an explicit, reviewed procedure, and that procedure is **not yet implemented**. Until it exists, do not run `host setup` or `fleet setup` against its data root, and do not erase activation or recovery evidence to make fresh setup pass. Historical records remain inspectable with `host releases`, `host status` and `migration status`.
