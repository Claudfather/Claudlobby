# Getting started

Prepare one managed Git checkout and Python environment, then prepare the first fleet through the same updater used for later changes. Installation and host data must have separate, dedicated absolute paths. Existing hosts with activation or recovery history need a reviewed migration; do not erase that evidence to use fresh setup.

## Prerequisites

Use a local operator terminal outside a bot or managed-service context. Choose the OS user deliberately: bots inherit its files, credentials and permissions. Read the [safety model](../README.md#safety-model).

Provision Git, CPython 3.10 or newer with working `venv` and `ssl`, `tmux`, `jq`, Claude Code, and a working native **user** manager (launchd on macOS or systemd user services on Linux). On Debian, creating a venv may require the administrator-provided `python3-venv` package. Unattended Linux operation also needs administrator-configured user lingering. Setup installs none of those prerequisites and never invokes `sudo`.

### Prove the Python executable once

Set `PYTHON` to an explicit executable outside the future installation and data directories. Keep the resolved path printed by this check. A version string or `import venv` alone does not prove that a usable environment can be created.

```bash
PYTHON="/absolute/path/to/python3"
PYTHON=$("$PYTHON" -I -c 'import sys; from pathlib import Path; assert sys.version_info >= (3, 10); print(Path(sys.executable).resolve(strict=True))') &&
PROBE=$(mktemp -d) &&
"$PYTHON" -I -m venv "$PROBE/venv" &&
"$PROBE/venv/bin/python" -I -c 'import encodings, plistlib, ssl, sys; assert sys.prefix != sys.base_prefix; print(sys.version)'
```

Stop if any check fails. Remove only your own probe directory afterward.

## 1. Prepare the managed installation

Obtain a trusted source checkout or source export containing the Python package. It is needed to run the stdlib bootstrap module, not as the eventual runtime. Supply the trusted remote and ref explicitly; setup does not discover a latest version or an ambient repository. The ref resolves once to a full commit and retries retain that commit.

Set these values to your own absolute paths and trusted inputs. `INSTALL` and `DATA` must not overlap each other or the independent host interpreter. The bootstrap command runs from `TRUSTED_SOURCE`; do not add `-I` to that module invocation because isolation hides the source module.

```bash
TRUSTED_SOURCE="/absolute/trusted/claudlobby-source"
INSTALL="/absolute/new/claudlobby-installation"
DATA="/absolute/new/claudlobby-data"
REMOTE="https://example.invalid/your-trusted/claudlobby.git"
REF="your-reviewed-ref"
(cd "$TRUSTED_SOURCE" && "$PYTHON" -B -m claudlobby.bootstrap \
  --installation "$INSTALL" --root "$DATA" \
  --remote "$REMOTE" --ref "$REF" --interpreter "$PYTHON" --json)
```

Preparation can download hash-locked runtime dependencies. It creates `INSTALL/src`, the final `INSTALL/env`, and stable `INSTALL/bin/claudlobby` and `INSTALL/bin/claudlobby-update` entries. It installs no editable package, creates no fleet recorder or owner grants, and starts no agents. Retain the returned source revision. On interruption, inspect retained files and retry the same inputs; do not reset or clean unrelated files.

Use the stable CLI for the native prerequisite check and the same installation preparation:

```bash
CLI="$INSTALL/bin/claudlobby"
"$CLI" --root "$DATA" host setup --installation "$INSTALL" \
  --remote "$REMOTE" --ref "$REF" --interpreter "$PYTHON" --json
```

This is the same preparation helper, plus the public host prerequisite/native catalog check. Choose the native installation directory from its observed `install_directories`; do not guess a replacement if the manager is unavailable.

On macOS, also retain the observed native domain. A logged-in desktop terminal and an SSH/background session can select different domains (`gui/UID` and `user/UID`). Prepare new updates from the original domain. Continuing an interrupted update keeps its recorded domain, including when continuing a GUI-bound operation from a background session; it never moves the services implicitly. Preparation does not prove the agents will be available after a reboot: verify that separately in the intended login/session arrangement.

**Credential gate:** stop here until provider sign-in and the intended repository/tool access are independently verified for this OS user. Preparation is not authentication or proof of agent readiness. Keep secrets in the appropriate private `.env` tiers, never in Git or command arguments. Teams without channels need no Telegram credentials. The optional `fleet.yaml.seed` is for an operator who explicitly chooses Telegram; complete its placeholders and channel prerequisites before using it.

## 2. Prepare the first fleet

Provide an authored `fleet.yaml` whose `fleet.name` matches `--fleet`. See the [schema](fleet-yaml-schema.md). Set `NATIVE_DIRECTORY` to the exact observed directory and `CONFIG` to your reviewed authored file.

```bash
NATIVE_DIRECTORY="/absolute/observed/user-unit-directory"
CONFIG="/absolute/private/fleet.yaml"
"$CLI" --root "$DATA" --fleet example fleet setup --config "$CONFIG" \
  --install-directory "$NATIVE_DIRECTORY" --plan-only --json
```

Alternatively, the [first-team template](first-team-setup.md) creates a lead, engineer and reviewer for an explicitly approved repository and goal. Choose one fleet setup command, not both.

`--plan-only` prepares the real update record, candidate configuration and dependencies. It requires the native directory and starts no native updater or agents. Review the returned request ID, target revision, configuration fingerprint, candidate configuration and consumer scope. This is a retained operation, not a disposable preview. Do not rerun fleet setup to replace it.

**Agent-start gate:** stop before the next command until you approve the reviewed scope and possible provider usage. A prepared result is not an active or healthy fleet.

## 3. Continue the original operation

```bash
"$CLI" --root "$DATA" host update status --json
"$CLI" --root "$DATA" host update continue --json
```

Continue uses the original request. It may enroll native services, initialize the fleet recorder, publish the committed configuration and start approved consumers. After a timeout or lost response, inspect status and continue that same operation; unknown effects are not permission to create another request or resend work.

Check `host doctor`, `fleet status`, and meaningful bot/provider behavior separately. A committed configuration or successful dispatch does not prove startup health. Later source/configuration changes use `host update apply FULL_COMMIT`; preserve pending operations before requesting another target. Existing-host migration and browser first-team cold-start acceptance remain separate work, not outcomes established by this walkthrough.
