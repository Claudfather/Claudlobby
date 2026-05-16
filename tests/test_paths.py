from pathlib import Path

from claudlobby.paths import Paths


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
    assert paths.base_post_actions == tmp_path / "library" / "post_actions"
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
