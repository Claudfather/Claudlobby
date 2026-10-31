---
name: setup
description: "Prepare a computer for website-connected first-team setup, or an explicitly requested operator path."
argument-hint: "[--check-only]"
---

# Setup

**The default `/setup` route is [First-team setup](../../../documentation/first-team-setup.md):** prepare one computer for a website-connected lead/engineer/reviewer team, without Telegram. Use [`documentation/getting-started.md`](../../../documentation/getting-started.md) only for its release assembly, then return to the browser route. Do not infer a request for the Telegram seed from a bare `/setup`.

First check noncredential native prerequisites, then assemble a trusted exact compatible release using an available bundle or the explicitly chosen source-build alternative. If no trusted inputs are available, report that gate; do not invent a download or activate a seed instead. Website setup publication may still be gated during candidate assembly/qualification. The checkout is a build input; the installed release and mutable host data live outside it. Do not run checkout `generate` or the retired `setup-system` installer as a substitute for activation.

Assembly starts no bots and does not prove access or public availability. After assembly, follow the first-team guide's provider/repository, native identity/ingress, workspace and setup-publication gates before owner or lifecycle operations. Stop there if access is unverified or browser setup is unavailable; never borrow credentials or start bots to discover whether access works. Owner pairing, workspace binding and exact setup approval are separate local operations; browser Launch can start native services and agreed provider usage. Native folder/tool approval may still need the human at that computer. Keep a lost launch's original identifiers and inspect its receipt rather than resending. A recorded launch does not establish team readiness or authorize merges/deployments.

The supervised first-team task/review/feedback trial is complete; unattended newcomer onboarding and the revised instructions' cold-host acceptance are not established.

For `--check-only`, check the host prerequisites and report what is missing without making changes. The host needs working Python with `venv` and `ssl`, `git` for a source build, `tmux`, `jq`, an installed Claude Code CLI and a working launchd user domain or systemd user manager. Report provider authentication as a separate continuation gate. Linux unattended operation additionally needs user lingering enabled through the host administrator. If the intended fleet uses Telegram, verify its channel plugin and token provisioning. Optional jobs may require their own tools; do not describe Node, `jq`, `gh`, or Claudron as installed by host setup.

## Optional operator path

Only when the user explicitly chooses the Telegram seed/direct CLI, follow the optional sections of Getting started. `fleet.yaml.seed` is not the default website route. A source build used for the browser route ends after assembly and returns to First-team setup; it does not run steps 3–5 below.

1. Build a wheel, download dependency wheels, construct the hash lock, and install the bootstrap wheel in a separate build directory. Follow the walkthrough's outside-checkout installed-wheel check; stop on a missing or wrong installation. Verify the wheel and lock are present before assembly.
2. Run the bootstrap wheel's `claudlobby --root DATA host setup --wheel WHEEL --dependency-lock LOCK --wheelhouse DIR --interpreter PYTHON --json`. Use the returned sealed CLI and native install directory; host setup assembles but does not select or start a release.
3. Copy and complete `fleet.yaml.seed` and `.env.seed.example` in their authoring locations. Replace all placeholders (`-1234567890` is only a fake group-ID example) and keep secrets in the fleet `.env`, never in CLI arguments.
4. Run the returned release CLI's `claudlobby --root DATA --fleet NAME fleet setup --config FILE --install-directory DIR`. This stages the host and activates the fleet and host jobs through the recorded activation owner. A changed existing manifest needs `--replace-config`; an interrupted activation needs explicit inspection of `host status` before another attempt.
5. Check `host doctor` and `host status`, then verify the current bot session's channel response separately; an old startup log or another session's poller is not readiness. Report assembled, selected, active, and observed runtime states distinctly.

For later changes, use `config plan`, `config diff`, and `host activate` as documented in the walkthrough. Do not silently install host packages, plugins, or system configuration on the user's behalf outside the requested setup flow.
