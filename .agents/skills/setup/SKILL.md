---
name: setup
description: "Bootstrap a sealed Claudlobby release and activate its first fleet."
argument-hint: "[--check-only]"
---

# Setup

Use [`documentation/getting-started.md`](../../../documentation/getting-started.md) as the executable cold-host walkthrough. The checkout is a build input; the installed release and mutable host data must live outside it. Do not run checkout `generate` or the retired `setup-system` installer as a substitute for activation.

For `--check-only`, check the host prerequisites and report what is missing without making changes. The host needs working Python with `venv` and `ssl`, `git` for a source build, `tmux`, an installed and authenticated Claude Code CLI, and a working launchd user domain or systemd user manager. Linux unattended operation additionally needs user lingering enabled through the host administrator. If the intended fleet uses Telegram, verify its channel plugin and token provisioning. Optional jobs may require their own tools; do not describe Node, `jq`, `gh`, or Claudron as installed by host setup.

For setup, follow the walkthrough in order:

1. Build a wheel, download dependency wheels, construct the hash lock, and install the bootstrap wheel in a separate build directory. Follow the walkthrough's outside-checkout installed-wheel check; stop on a missing or wrong installation. Verify the wheel and lock are present before assembly.
2. Run the bootstrap wheel's `claudlobby --root DATA host setup --wheel WHEEL --dependency-lock LOCK --wheelhouse DIR --interpreter PYTHON --json`. Use the returned sealed CLI and native install directory; host setup assembles but does not select or start a release.
3. Copy and complete `fleet.yaml.seed` and `.env.seed.example` in their authoring locations. Replace all placeholders (`-1234567890` is only a fake group-ID example) and keep secrets in the fleet `.env`, never in CLI arguments.
4. Run the returned release CLI's `claudlobby --root DATA --fleet NAME fleet setup --config FILE --install-directory DIR`. This stages the host and activates the fleet and host jobs through the recorded activation owner. A changed existing manifest needs `--replace-config`; an interrupted activation needs explicit inspection of `host status` before another attempt.
5. Check `host doctor` and `host status`, then verify the current bot session's channel response separately; an old startup log or another session's poller is not readiness. Report assembled, selected, active, and observed runtime states distinctly.

For later changes, use `config plan`, `config diff`, and `host activate` as documented in the walkthrough. Do not silently install host packages, plugins, or system configuration on the user's behalf outside the requested setup flow.
