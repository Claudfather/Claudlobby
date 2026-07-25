from pathlib import Path

import pytest

from claudlobby.paths import Paths


def test_detect_names_env_source_when_root_invalid(tmp_path: Path, monkeypatch) -> None:
    # A stale CLAUDLOBBY_ROOT must fail loudly and say the env var chose the
    # detection start — not masquerade as a cwd-walk failure.
    bogus = tmp_path / "not-a-root"
    bogus.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(bogus))

    with pytest.raises(FileNotFoundError, match="CLAUDLOBBY_ROOT"):
        Paths.detect()


def _make_root(base: Path, name: str = "claudlobby") -> Path:
    """Create a directory carrying the root markers (library/ + lib/)."""
    root = base / name
    (root / "library").mkdir(parents=True)
    (root / "lib").mkdir()
    return root


def test_detect_honors_claudlobby_root_env_from_foreign_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    root = _make_root(tmp_path)
    foreign = tmp_path / "foreign-cwd"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(root))

    assert Paths.detect().root == root.resolve()


def test_detect_walks_up_from_cwd_when_env_unset(tmp_path: Path, monkeypatch) -> None:
    root = _make_root(tmp_path)
    nested = root / "local" / "some-fleet"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert Paths.detect().root == root.resolve()


def test_detect_explicit_hint_beats_env(tmp_path: Path, monkeypatch) -> None:
    env_root = _make_root(tmp_path, "env-root")
    hint_root = _make_root(tmp_path, "hint-root")
    monkeypatch.setenv("CLAUDLOBBY_ROOT", str(env_root))

    assert Paths.detect(hint=hint_root).root == hint_root.resolve()


def test_base_expertise_returns_base_library_subdir(tmp_path: Path) -> None:
    paths = Paths(root=tmp_path)

    assert paths.base_expertise == tmp_path / "library" / "expertise"


def test_base_accessors_point_at_public_library(tmp_path: Path) -> None:
    paths = Paths(root=tmp_path)

    assert paths.base_skills == tmp_path / "library" / "skills"
    assert paths.base_mcp == tmp_path / "library" / "mcp"
    assert paths.base_integrations == tmp_path / "library" / "integrations"
    assert paths.base_guardrails == tmp_path / "library" / "guardrails"
    assert paths.base_protocols == tmp_path / "library" / "protocols"
    assert paths.base_resources == tmp_path / "library" / "resources"
    assert paths.base_lessons == tmp_path / "library" / "lessons"
    assert paths.base_voices == tmp_path / "voices"


def test_legacy_base_path_aliases_are_removed(tmp_path: Path) -> None:
    paths = Paths(root=tmp_path)

    for name in (
        "library",
        "expertise",
        "skills",
        "mcp",
        "integrations",
        "guardrails",
        "principles",
        "protocols",
        "resources",
        "lessons",
        "post_actions",
        "voices",
    ):
        assert not hasattr(paths, name)
