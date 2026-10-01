# Install patterns

Claudlobby uses a sealed release for both supported native managers. The [cold-host walkthrough](getting-started.md) builds a wheel and dependency lock, runs `claudlobby host setup` to assemble the release, then uses that release's `claudlobby --fleet NAME fleet setup --config FILE --install-directory PATH` to compose and activate the fleet **and its host jobs**. `host setup` checks `tmux`, Claude Code, and the user manager; install and authenticate those prerequisites first. For a Telegram fleet, install its channel plugin and configure its token as the walkthrough describes. Tools used by optional jobs, such as `gh`, `jq`, Node, or Claudron, remain prerequisites of those jobs rather than implicit host-setup installs.

| Manager | Install directory | Persistence |
|---|---|---|
| macOS launchd | `$HOME/Library/LaunchAgents` | The current user domain owns the LaunchAgents. |
| Linux systemd user | `$HOME/.config/systemd/user` | Enable user lingering through the host administrator if jobs must survive logout. |

Use the exact install directory reported by `host setup`. `fleet setup` activates through the recorded host owner and checks the native result. Later changes use `config plan`, `config diff`, and `host activate`; see [Getting started](getting-started.md#4-activate-and-diagnose). Direct unit installers are private native primitives, not an alternative deployment path.
