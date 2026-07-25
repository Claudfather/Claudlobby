"""Self-referential telegram token_env must not scaffold a poisoning empty export (#750).

Follow-up to #749 (the start-bot runtime fix). When `token_env` names the telegram
plugin's own read var (`TELEGRAM_BOT_TOKEN` — the documented default in
fleet.yaml.example), scaffolding an empty `export TELEGRAM_BOT_TOKEN=` into the bot
.env is exactly the shape that poisons the poller: the plugin treats a
defined-but-empty value as authoritative, skips its channel-dir .env fallback, and
exits before writing bot.pid → dead inbound bridge.

Defense in depth at the compositor: `collect_env_contracts` skips that var (mirroring
the `provided_by: composer` skip) so the plugin's channel-dir .env fallback works, and
the validator drops its "not set" warning for it (the token's real home — the plugin
channel .env — is not a tier validate inspects, so the check would false-alarm on the
common default). A DISTINCT token_env name is unaffected; an operator-filled value is
still preserved by the merge.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from claudlobby.composer import collect_env_contracts, scaffold_env_files
from claudlobby.config import load_fleet
from claudlobby.paths import Paths
from claudlobby.validator import validate

# selfref uses the plugin's own read var (self-referential); distinct uses its own name.
_FLEET = """\
fleet:
  name: test-fleet
  service_prefix: com.test
  bots:
    selfref:
      expertise: [eng]
      telegram:
        handle: selfref_bot
        token_env: TELEGRAM_BOT_TOKEN
    distinct:
      expertise: [eng]
      telegram:
        handle: distinct_bot
        token_env: DISTINCT_TG_TOKEN
"""


def _setup(tmp_path: Path) -> tuple[Path, Paths]:
    root = tmp_path / "claudlobby"
    root.mkdir()
    (root / "fleet.yaml").write_text(dedent(_FLEET))
    (root / "library" / "expertise").mkdir(parents=True)
    (root / "library" / "expertise" / "eng.md").write_text("# Eng\n\nBuild.\n")
    for b in ("selfref", "distinct"):
        (root / "runtime" / "bots" / b).mkdir(parents=True)
    return root, Paths(root=root, fleet_dir=root)


def test_self_referential_token_env_not_collected(tmp_path):
    root, paths = _setup(tmp_path)
    fleet, _md = load_fleet(root / "fleet.yaml")
    names = {v.name for v in collect_env_contracts(fleet, paths)}
    assert "TELEGRAM_BOT_TOKEN" not in names  # self-ref -> never a scaffold stub
    assert "DISTINCT_TG_TOKEN" in names  # a distinct name stays operator-facing


def test_self_referential_token_env_not_scaffolded(tmp_path):
    root, paths = _setup(tmp_path)
    fleet, _md = load_fleet(root / "fleet.yaml")
    scaffold_env_files(fleet, paths, log=lambda m: None)
    # Bot-tier vars land in EVERY bot .env; assert the poisoning stub is in none of
    # them, while the distinct name is still scaffolded for the operator to fill.
    for b in ("selfref", "distinct"):
        env_text = (root / "runtime" / "bots" / b / ".env").read_text()
        assert "TELEGRAM_BOT_TOKEN" not in env_text
        assert "DISTINCT_TG_TOKEN" in env_text


def test_operator_filled_self_ref_token_is_preserved(tmp_path):
    # Cold-start contract: if an operator DID fill a real value in the bot .env,
    # re-scaffolding preserves it verbatim — never clobbered, never re-stubbed empty.
    root, paths = _setup(tmp_path)
    fleet, _md = load_fleet(root / "fleet.yaml")
    env_path = root / "runtime" / "bots" / "selfref" / ".env"
    env_path.write_text("export TELEGRAM_BOT_TOKEN=real-token-123\n")
    scaffold_env_files(fleet, paths, log=lambda m: None)
    assert "export TELEGRAM_BOT_TOKEN=real-token-123" in env_path.read_text()


def test_self_referential_token_env_no_validator_warning(tmp_path, monkeypatch):
    # The plugin's channel-dir .env (the self-ref token's real home) is not a tier
    # validate inspects, so a "not set" warning would false-alarm on the documented
    # default. Suppressed for self-ref; kept for a genuinely-unconfigured distinct name.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISTINCT_TG_TOKEN", raising=False)
    root, paths = _setup(tmp_path)
    fleet, _md = load_fleet(root / "fleet.yaml")
    report = validate(fleet, paths)
    warns = "\n".join(report.warnings)
    assert "TELEGRAM_BOT_TOKEN" not in warns  # self-ref: suppressed
    assert "DISTINCT_TG_TOKEN" in warns  # distinct + unconfigured: still warns
