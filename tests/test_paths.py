from pathlib import Path

import pytest

from claudlobby.context import load_context, resolve_context, resolve_paths
from claudlobby.paths import Paths
from claudlobby.resources import PackageResources


@pytest.fixture
def package(tmp_path: Path) -> PackageResources:
    """Explicit package assets separate from every writable data fixture."""
    install = tmp_path / "installed" / "claudlobby"
    assets = install / "_resources"
    directories = [assets / name for name in ("library", "voices", "templates", "seeds")]
    directories.append(install / "_runtime_scripts")
    for directory in directories:
        directory.mkdir(parents=True)
    system_yaml = install / "system.yaml"
    system_yaml.write_text("{}\n")
    return PackageResources(*directories, system_yaml, "test-artifact", None, "test-content")


def _fleet(directory: Path, name: str = "example") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "fleet.yaml"
    manifest.write_text(
        f"fleet:\n  name: {name}\n  manager: alice\n  system_defaults: false\n"
        "  bots:\n    alice:\n      expertise: [software-engineering]\n"
    )
    return manifest


def test_explicit_data_root_beats_env_without_checkout_markers(tmp_path, monkeypatch, package):
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(tmp_path / "other"))
    root = tmp_path / "new-data"
    paths = Paths.detect(hint=root, package=package)
    assert paths.root == root.resolve()
    assert paths.lib == package.native
    assert not root.exists()  # resolution does not bootstrap or write anything


def test_environment_root_is_authoritative_from_foreign_cwd(tmp_path, monkeypatch, package):
    root = tmp_path / "data"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))
    assert Paths.detect(package=package).root == root
    monkeypatch.setenv("CLAUDLOBBY_ROOT", "")
    with pytest.raises(ValueError, match="CLAUDLOBBY_ROOT is empty"):
        Paths.detect(package=package)


def test_explicit_root_file_does_not_walk_to_parent(tmp_path, monkeypatch, package):
    (tmp_path / "state" / "plane").mkdir(parents=True)
    manifest = _fleet(tmp_path / "data")
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(manifest))
    with pytest.raises(ValueError, match="not a directory"):
        Paths.detect(package=package)


def test_cwd_discovery_requires_one_host_data_layout(tmp_path, monkeypatch, package):
    monkeypatch.delenv("CLAUDLOBBY_ROOT", raising=False)
    root = tmp_path / "data"
    fleet_dir = root / "local" / "system" / "example"
    _fleet(fleet_dir)
    monkeypatch.chdir(fleet_dir)
    assert Paths.detect(fleet="example", package=package).root == root
    assert Paths.detect(fleet="example", package=package).fleet_dir == fleet_dir

    # Two host-scoped layouts in the ancestor chain are not a nearest-root rule.
    (fleet_dir / "state" / "plane").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="--root DATA_ROOT"):
        Paths.detect(package=package)


def test_cwd_lone_fleet_or_checkout_markers_do_not_select_host(tmp_path, monkeypatch, package):
    monkeypatch.delenv("CLAUDLOBBY_ROOT", raising=False)
    directory = tmp_path / "ambiguous"
    _fleet(directory)
    (directory / "library").mkdir()
    (directory / "lib").mkdir()
    monkeypatch.chdir(directory)
    with pytest.raises(FileNotFoundError, match="--root DATA_ROOT"):
        Paths.detect(package=package)


def test_nested_real_fleet_beats_flat_husk_but_duplicates_refuse(tmp_path, package):
    root = tmp_path / "data"
    flat = root / "local" / "example"
    flat.mkdir(parents=True)
    nested = root / "local" / "system" / "example"
    _fleet(nested)
    assert Paths.detect(root, "example", package=package).fleet_dir == nested
    _fleet(flat)
    with pytest.raises(ValueError, match="two depths"):
        Paths.detect(root, "example", package=package)


def test_vault_fleet_and_root_fleet_by_name(tmp_path, monkeypatch, package):
    monkeypatch.setattr("claudlobby.paths._HAS_CLAUDRON", False)
    root = tmp_path / "data"
    _fleet(root, "root-fleet")
    vault = tmp_path / "vault"
    _fleet(vault / "example")
    (root / ".claudron").write_text(f"vault={vault}\n")
    paths = Paths.detect(root, "example", package=package)
    assert paths.fleet_dir == vault / "example"
    assert paths.source_dir == vault / "example"
    assert paths.vault_root == vault
    assert Paths.detect(root, "root-fleet", package=package).fleet_dir is None
    with pytest.raises(FileNotFoundError, match="Fleet overlay not found"):
        Paths.detect(root, "missing", package=package)
    with pytest.raises(ValueError, match="not a filesystem path"):
        Paths.detect(root, "../vault/example", package=package)


def test_checkout_data_root_serves_host_and_local_fleets_but_not_root_mode(tmp_path, package):
    root = tmp_path / "old-checkout"
    _fleet(root / "local" / "example")
    (root / "claudlobby").mkdir()
    (root / "claudlobby" / "__init__.py").write_text("")
    (root / "library" / "skills").mkdir(parents=True)
    # The conversion runbook keeps the checkout as the data root: host-level
    # scope and local fleets resolve, and a local fleet's overlay is its own.
    assert Paths.detect(root, package=package).root == root.resolve()
    local = Paths.detect(root, "example", package=package)
    assert local.fleet_dir == root / "local" / "example"
    assert local.overlay_library == root / "local" / "example" / "library"
    # A root-mode fleet would let the checkout's library outrank the release.
    _fleet(root, "root-fleet")
    for fleet in ("root-fleet", None):
        with pytest.raises(ValueError, match="source checkout"):
            Paths.detect(root, fleet, package=package)
    assert Paths.detect(root, "example", package=package).fleet_dir == root / "local" / "example"


def test_base_accessors_and_root_overlay_precedence(tmp_path, package):
    root = tmp_path / "data"
    paths = Paths(root=root, package=package)
    for name in ("expertise", "skills", "mcp", "integrations", "guardrails",
                 "protocols", "resources", "lessons"):
        assert getattr(paths, f"base_{name}") == package.library / name
    assert paths.base_voices == package.voices
    assert paths.base_templates == package.templates
    assert paths.overlay_library == root / "library"
    assert paths.overlay_voices == root / "voices"
    assert paths.overlay_templates == root / "templates"
    assert paths.source_dir == root

    (package.library / "expertise").mkdir()
    (package.library / "expertise" / "role.md").write_text("packaged")
    override = root / "library" / "expertise" / "role.md"
    override.parent.mkdir(parents=True)
    override.write_text("overlay")
    assert paths.find_library_file("expertise", "role") == override
    (package.voices / "voice.md").write_text("packaged")
    assert paths.find_voice_file("voices/voice.md") == package.voices / "voice.md"
    paths.overlay_voices.mkdir()
    (paths.overlay_voices / "voice.md").write_text("overlay")
    assert paths.find_voice_file("voice.md") == paths.overlay_voices / "voice.md"


def test_seed_template_does_not_become_writable_config_root(tmp_path, package):
    paths = Paths(root=tmp_path / "data", package=package, seed=True)
    assert paths.fleet_yaml == package.seeds / "fleet.yaml.seed"
    assert paths.fleet_config_dir == paths.root
    assert paths.projects_yaml == paths.root / "projects.yaml"
    assert paths.env_file == paths.root / ".env"
    assert paths.runtime == paths.root / "runtime" / "seed"


def test_writable_guard_rejects_package_ancestors_and_symlink_escapes(tmp_path, package):
    root = tmp_path / "data"
    root.mkdir()
    paths = Paths(root=root, package=package)
    target = root / "library" / "skills" / "new" / "SKILL.md"
    assert paths.assert_writable(target) == target
    alias = tmp_path / "data-alias"
    alias.symlink_to(root, target_is_directory=True)
    assert paths.assert_writable(alias / "new.md") == root / "new.md"
    for destination in (package.library / "new.md", package.native,
                        package.system_yaml.parent / "new.py", tmp_path):
        with pytest.raises(ValueError, match="package-owned"):
            paths.assert_writable(destination)
    (root / "package-link").symlink_to(package.library, target_is_directory=True)
    with pytest.raises(ValueError, match="package-owned"):
        paths.assert_writable(root / "package-link" / "new.md")
    outside = tmp_path / "unselected"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside selected"):
        paths.assert_writable(root / "escape" / "new.md")
    vault_source = tmp_path / "vault" / "example"
    vault_paths = Paths(root=root, package=package, fleet_dir=vault_source)
    assert vault_paths.assert_writable(vault_source / "voices" / "new.md") == (
        vault_source / "voices" / "new.md")


def test_explicit_output_root_allows_staging_without_rebasing_data(tmp_path, package):
    paths = Paths(root=tmp_path / "data", package=package)
    stage = tmp_path / "stage"
    stage.mkdir()
    target = stage / "bot" / "bot.conf"
    assert paths.assert_writable(target, output_root=stage) == target
    assert paths.runtime == tmp_path / "data" / "runtime"
    with pytest.raises(ValueError, match="outside selected"):
        paths.assert_writable(target)
    (stage / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="outside selected"):
        paths.assert_writable(stage / "escape" / "new", output_root=stage)
    (stage / "data-link").symlink_to(paths.root, target_is_directory=True)
    assert paths.assert_writable(stage / "data-link" / "new", output_root=stage) == (
        paths.root / "new")
    with pytest.raises(ValueError, match="package-owned"):
        paths.assert_writable(package.native / "new", output_root=package.native)


def test_context_checks_loaded_fleet_and_bot(tmp_path, package):
    root = tmp_path / "data"
    _fleet(root)
    context = resolve_context(root=root, fleet="example", bot="alice", package=package)
    assert context.fleet.name == "example"
    assert context.bot_id == "alice"
    with pytest.raises(ValueError, match="not declared"):
        resolve_context(root=root, bot="other", package=package)
    with pytest.raises(ValueError, match="requested fleet"):
        load_context(context.paths, fleet="other")
    _fleet(root / "local" / "selected", "different-name")
    with pytest.raises(ValueError, match="requested fleet"):
        resolve_context(root=root, fleet="selected", package=package)


def test_production_resolver_does_not_fallback_to_checkout(tmp_path, monkeypatch):
    def unavailable():
        raise RuntimeError("package is not built")

    monkeypatch.setattr("claudlobby.context.get_resources", unavailable)
    (tmp_path / "library").mkdir()
    (tmp_path / "lib").mkdir()
    with pytest.raises(RuntimeError, match="not built"):
        resolve_paths(root=tmp_path)


def test_package_is_required_keyword_only_and_legacy_aliases_stay_removed(tmp_path, package):
    with pytest.raises(TypeError):
        Paths(root=tmp_path)
    paths = Paths(root=tmp_path / "data", package=package)
    for name in ("library", "expertise", "skills", "mcp", "integrations",
                 "guardrails", "principles", "protocols", "resources", "lessons",
                 "post_actions", "voices"):
        assert not hasattr(paths, name)
