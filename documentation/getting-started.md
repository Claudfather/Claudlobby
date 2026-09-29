# Getting started

This is the installed-release path for a new host. Build one candidate wheel and its exact offline dependency inputs, assemble it under a host data root, then use the sealed CLI to install and activate a fleet. The checkout is a build input; running bots use the release's copied interpreter, packaged library, and native scripts.

## Prerequisites

- A working CPython accepted by the candidate wheel (`python3 -c 'import plistlib, ssl, venv'` must succeed), `git` for a source build, and enough disk for a copied Python environment and dependency wheels.
- `tmux` and the `claude` executable on `PATH`; Claude Code must be installed and authenticated for the user who will run the bots.
- A working **user** manager: launchd in the current macOS user domain, or `systemctl --user` on Linux. For unattended Linux boots, enable user lingering using your host's normal administrative procedure. `host setup` observes the manager; it does not install packages, enable lingering, or change system settings.
- For the seed's Telegram bot, install the Telegram channel plugin (`claude plugin install telegram@claude-plugins-official`), obtain a token from BotFather, and know your Telegram user and group IDs. A fleet without Telegram may author a different manifest.

The copied interpreter still uses the host's standard library and system libraries. A wheelhouse prepared for another platform or Python version may be unusable here.

## 1. Prepare the release inputs

Use a committed Claudlobby source checkout for this example. There is no hosted release bundle or built-in lock-generation command. These commands follow the repository's [offline assembly CI rehearsal](../.github/workflows/test.yml) and its [hash-lock construction](../tests/release_assembly_smoke.py): network access prepares inputs; assembly itself uses only local wheels.

```bash
git clone https://github.com/Claudfather/Claudlobby.git
cd Claudlobby

PYTHON="$(command -v python3)"                 # choose a working host CPython
"$PYTHON" -c 'import plistlib, ssl, venv'
WORK="$HOME/.local/share/claudlobby-build"
DATA="$HOME/.local/share/claudlobby-data"    # outside the checkout
mkdir -p "$WORK/dist" "$WORK/wheelhouse"
"$PYTHON" -m venv "$WORK/bootstrap"
"$WORK/bootstrap/bin/python" -m pip install 'build>=1,<2' 'setuptools>=68' wheel
"$WORK/bootstrap/bin/python" -m build --wheel --no-isolation --outdir "$WORK/dist" .
"$WORK/bootstrap/bin/python" -m pip download --only-binary=:all: \
  --dest "$WORK/wheelhouse" "$WORK"/dist/*.whl
"$WORK/bootstrap/bin/python" -m pip install --no-index \
  --find-links "$WORK/wheelhouse" "$WORK"/dist/*.whl
```

The bootstrap venv runs the installed wheel. The target release is assembled separately under `DATA`. Prepare its hash lock from downloaded wheel metadata and bytes, excluding the Claudlobby wheel, which the assembler installs separately:

```bash
"$WORK/bootstrap/bin/python" - "$WORK/wheelhouse" "$WORK/dependency.lock" <<'PY'
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path
import sys
from zipfile import ZipFile

rows = []
for wheel in sorted(Path(sys.argv[1]).glob("*.whl")):
    with ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise SystemExit(f"invalid wheel metadata: {wheel.name}")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    if metadata["Name"].lower() != "claudlobby":
        rows.append(f"{metadata['Name']}=={metadata['Version']} "
                    f"--hash=sha256:{sha256(wheel.read_bytes()).hexdigest()}")
if not rows:
    raise SystemExit("dependency wheelhouse is empty")
Path(sys.argv[2]).write_text("\n".join(rows) + "\n")
PY
```

The assembler refuses URLs, source distributions, unpinned requirements, missing SHA-256 hashes, and dependency resolution outside this wheelhouse. Keep the wheel, lock, and wheelhouse together for repeatable assembly.

## 2. Assemble a sealed host release

Choose the single wheel built above. `host setup` verifies package resources, the user manager, `tmux`, and `claude`; it assembles an immutable release and reports its CLI and native search paths. It **does not select the release or start a bot**.

```bash
set -- "$WORK"/dist/*.whl
[ "$#" -eq 1 ] || { echo 'expected exactly one candidate wheel' >&2; exit 1; }
WHEEL="$1"
"$WORK/bootstrap/bin/claudlobby" --root "$DATA" host setup \
  --wheel "$WHEEL" --dependency-lock "$WORK/dependency.lock" \
  --wheelhouse "$WORK/wheelhouse" --interpreter "$WORK/bootstrap/bin/python" \
  --json > "$WORK/host-setup.json"
RELEASE_CLI=$("$WORK/bootstrap/bin/python" -c \
  'import json,sys; result=json.load(open(sys.argv[1])); assert result["ok"]; print(result["data"]["cli"])' \
  "$WORK/host-setup.json")
"$RELEASE_CLI" --help
```

Use the exact CLI reported by setup for the remaining commands. If setup refuses native manager access, repair the host's user-manager session first; a guessed unit directory cannot replace that proof.

## 3. Author the first fleet

The wheel ships `fleet.yaml.seed` and `.env.seed.example`. Copy them to **authoring locations**, then edit the placeholders. The seed's declared fleet name is `seed` and its manager bot is `claudfather`. `fleet.yaml.example` is a field reference, not a minimal first fleet.

```bash
SEEDS=$("$WORK/bootstrap/bin/python" -c \
  'from claudlobby.resources import get_resources; print(get_resources().seeds)')
cp "$SEEDS/fleet.yaml.seed" "$WORK/fleet.yaml"
mkdir -p "$DATA/local/seed"
cp "$SEEDS/.env.seed.example" "$DATA/local/seed/.env"
chmod 600 "$DATA/local/seed/.env"
"${EDITOR:-vi}" "$WORK/fleet.yaml"
"${EDITOR:-vi}" "$DATA/local/seed/.env"
```

Replace every `REPLACE_ME` in the manifest, including the Telegram handle and user/group IDs. Put the matching `TELEGRAM_TOKEN_CLAUDFATHER` in the fleet `.env`; a GitHub token is optional. The `.env` stays in the data overlay and is never passed as a CLI argument.

## 4. Activate and diagnose

Set `USER_UNIT_DIR` to a path reported in `host-setup.json` under `data.install_directories`. For a normal macOS user, that is `$HOME/Library/LaunchAgents`; for Linux user systemd, use `$HOME/.config/systemd/user`. Run from the same user-manager domain observed by setup.

```bash
case "$(uname -s)" in
  Darwin) USER_UNIT_DIR="$HOME/Library/LaunchAgents" ;;
  Linux)  USER_UNIT_DIR="$HOME/.config/systemd/user" ;;
  *) echo 'unsupported native manager' >&2; exit 1 ;;
esac
mkdir -p "$USER_UNIT_DIR"
"$RELEASE_CLI" --root "$DATA" --fleet seed fleet setup \
  --config "$WORK/fleet.yaml" --install-directory "$USER_UNIT_DIR"
"$RELEASE_CLI" --root "$DATA" host doctor
"$RELEASE_CLI" --root "$DATA" host status
```

`fleet setup` copies the authored manifest to `$DATA/local/seed/fleet.yaml`, stages all declared host fleets, and activates through the release owner. It may stop or start supervised processes, so run it when you intend to bring the fleet up. A different existing target manifest requires `--replace-config`; an unchanged, already active plan is not a new activation. A failed activation retains its recorded pending step for explicit repair. `host doctor` reports configuration and host checks; verify the bot's channel and response separately.

For later config changes, edit an authoring file and stage and activate through the sealed CLI. Do not run checkout `generate` or legacy `lib/setup-fleet` against an active selected host: they write generated or native state outside the selected activation journal. The [fleet schema](fleet-yaml-schema.md) documents each manifest field.
