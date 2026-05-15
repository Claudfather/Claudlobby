"""Tests for knowledge retrieval engine."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from textwrap import dedent


from claudron.vault import detect
from claudron.knowledge import (
    _build_index,
    _load_index,
    lookup,
)


class TestScoring:
    def test_lookup_exact_title(self, vault_dir: Path):
        vault = detect(vault_dir)
        results = lookup("Auth Patterns Across Services", vault)
        assert len(results) >= 1
        assert results[0].score == 100
        assert results[0].match_type == "title"

    def test_lookup_title_substring(self, vault_dir: Path):
        vault = detect(vault_dir)
        results = lookup("auth patterns", vault)
        assert len(results) >= 1
        assert results[0].score >= 80
        assert results[0].match_type == "title"

    def test_lookup_tag_exact(self, vault_dir: Path):
        vault = detect(vault_dir)
        results = lookup("jwt", vault)
        assert len(results) >= 1
        assert results[0].match_type == "tag"
        assert results[0].score >= 60

    def test_lookup_content_match(self, vault_dir: Path):
        vault = detect(vault_dir)
        results = lookup("RS256", vault)
        assert len(results) >= 1
        # Tier B body match
        assert results[0].match_type in ("content", "heading")

    def test_lookup_no_match(self, vault_dir: Path):
        vault = detect(vault_dir)
        results = lookup("kubernetes helm chart", vault)
        assert results == []

    def test_multiword_tokenization(self, vault_dir: Path):
        vault = detect(vault_dir)
        # "checklist deploy" is NOT a substring of "Deploy Checklist",
        # so phrase match fails and multi-word tokenization fires.
        results = lookup("checklist deploy", vault)
        assert len(results) >= 1
        top = results[0]
        assert "deploy checklist" in top.doc.title.lower()
        # Must hit title via token-per-word (score 100 = 2×50), not body
        assert top.match_type == "title"
        assert top.score == 100

    def test_lookup_in_shared_vault(self, shared_vault: Path):
        """Lookup works in vaults using shared/ instead of _shared/."""
        vault = detect(shared_vault)
        results = lookup("Testing Guide", vault)
        assert len(results) >= 1
        assert results[0].score == 100


class TestTiers:
    def test_tier_ordering(self, vault_with_projects: Path):
        """Project-tier results sort before shared-tier at equal score."""
        # Add a shared doc with similar title
        (vault_with_projects / "_shared" / "knowledge" / "neon-general.md").write_text(
            dedent("""\
            ---
            title: Neon Connection Pooling
            type: knowledge
            status: active
            owner: mason
            tags: [neon, database]
            created: 2026-04-01
            updated: 2026-05-01
            ---

            # Neon Connection Pooling

            General neon pooling info.
        """)
        )
        vault = detect(vault_with_projects)
        results = lookup("neon", vault, project="storydump", limit=10)
        # Both should appear; project should be first (at equal score, project wins)
        project_results = [r for r in results if r.doc.tier.startswith("project:")]
        shared_results = [r for r in results if r.doc.tier == "shared"]
        assert len(project_results) >= 1
        assert len(shared_results) >= 1
        # First project result should come before first shared result
        first_project_idx = next(
            i for i, r in enumerate(results) if r.doc.tier.startswith("project:")
        )
        first_shared_idx = next(
            i for i, r in enumerate(results) if r.doc.tier == "shared"
        )
        # If scores are equal, project wins on tier priority
        if project_results[0].score == shared_results[0].score:
            assert first_project_idx < first_shared_idx


class TestLimits:
    def test_limit_respected(self, vault_dir: Path):
        # Add many docs
        for i in range(20):
            (vault_dir / "_shared" / "knowledge" / f"topic-{i}.md").write_text(
                dedent(f"""\
                ---
                title: Topic {i} About Auth
                type: knowledge
                status: active
                owner: alex
                tags: [auth, topic-{i}]
                created: 2026-04-01
                updated: 2026-05-01
                ---

                # Topic {i} About Auth

                Auth related content number {i}.
            """)
            )
        vault = detect(vault_dir)
        results = lookup("auth", vault, limit=5)
        assert len(results) <= 5


class TestFiltering:
    def test_skips_index_and_readme(self, vault_dir: Path):
        (vault_dir / "_shared" / "knowledge" / "INDEX.md").write_text(
            "# Index\n- stuff"
        )
        (vault_dir / "_shared" / "knowledge" / "README.md").write_text("# Readme")
        vault = detect(vault_dir)
        results = lookup("index", vault, limit=20)
        paths = [str(r.doc.source_path.name) for r in results]
        assert "INDEX.md" not in paths
        assert "README.md" not in paths

    def test_missing_frontmatter_uses_filename(self, vault_dir: Path):
        (vault_dir / "_shared" / "knowledge" / "no-frontmatter.md").write_text(
            "Just a plain markdown file about testing.\n"
        )
        vault = detect(vault_dir)
        results = lookup("no frontmatter", vault, limit=10)
        # Should find it by filename
        matching = [r for r in results if "no-frontmatter" in str(r.doc.source_path)]
        assert len(matching) >= 1
        assert matching[0].doc.title == "No frontmatter"

    def test_excludes_archived_by_default(self, vault_dir: Path):
        (vault_dir / "_shared" / "knowledge" / "old-auth.md").write_text(
            dedent("""\
            ---
            title: Old Auth Approach
            type: knowledge
            status: archived
            owner: mason
            tags: [auth]
            created: 2026-01-01
            updated: 2026-02-01
            ---

            # Old Auth Approach

            We used to do basic auth. Don't anymore.
        """)
        )
        vault = detect(vault_dir)
        results = lookup("Old Auth Approach", vault, limit=10)
        archived = [r for r in results if r.doc.title == "Old Auth Approach"]
        assert len(archived) == 0

        # But with include_archived
        results = lookup("Old Auth Approach", vault, limit=10, include_archived=True)
        archived = [r for r in results if r.doc.title == "Old Auth Approach"]
        assert len(archived) >= 1

    def test_excludes_expired_by_default(self, vault_dir: Path):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        (vault_dir / "_shared" / "knowledge" / "expired-doc.md").write_text(
            dedent(f"""\
            ---
            title: Expired Knowledge
            type: knowledge
            status: active
            owner: alex
            tags: [expired-test]
            created: 2026-01-01
            updated: 2026-01-01
            expires: {yesterday}
            ---

            # Expired Knowledge

            This doc has expired.
        """)
        )
        vault = detect(vault_dir)
        results = lookup("Expired Knowledge", vault, limit=10)
        expired = [r for r in results if r.doc.title == "Expired Knowledge"]
        assert len(expired) == 0

        results = lookup("Expired Knowledge", vault, limit=10, include_expired=True)
        expired = [r for r in results if r.doc.title == "Expired Knowledge"]
        assert len(expired) >= 1


class TestScoreMerge:
    def test_tier_b_merges_with_tier_a(self, vault_dir: Path):
        """When a doc scores low in Tier A and also matches body, scores merge."""
        # filename match only → score 30 (below TIER_A_THRESHOLD of 50)
        (vault_dir / "_shared" / "knowledge" / "caching.md").write_text(
            dedent("""\
            ---
            title: Caching Strategies
            type: knowledge
            status: active
            owner: alex
            tags: [performance]
            created: 2026-04-01
            updated: 2026-05-01
            ---

            # Caching Strategies

            Use caching for frequently accessed data.
        """)
        )
        vault = detect(vault_dir)
        # "caching" matches filename (30) in Tier A, and heading (70) in Tier B
        results = lookup("caching", vault, limit=10)
        caching_results = [r for r in results if "Caching" in r.doc.title]
        assert len(caching_results) >= 1
        # Score should be merged (30 + 70 = 100), not just 30
        assert caching_results[0].score > 30


class TestProjectVisibility:
    def test_unscoped_lookup_finds_project_docs(self, vault_with_projects: Path):
        """Tier B body search finds project docs even without --project."""
        vault = detect(vault_with_projects)
        # "serverless driver" appears only in the project doc body
        results = lookup("serverless driver", vault, limit=10)
        assert len(results) >= 1
        assert any(r.doc.tier.startswith("project:") for r in results)


class TestIndex:
    def test_index_build_and_load(self, vault_dir: Path):
        vault = detect(vault_dir)
        index = _build_index(vault)
        assert "entries" in index
        assert len(index["entries"]) == 2  # auth-patterns + deploy-checklist

        loaded = _load_index(vault)
        assert loaded is not None
        assert len(loaded["entries"]) == 2

    def test_index_stale_detection(self, vault_dir: Path):
        import time

        from claudron.vault import _clear_stale_cache

        vault = detect(vault_dir)
        _build_index(vault)
        loaded = _load_index(vault)
        assert loaded is not None

        # Touch a file to make index stale
        time.sleep(0.05)
        (vault_dir / "_shared" / "knowledge" / "auth-patterns.md").write_text(
            (vault_dir / "_shared" / "knowledge" / "auth-patterns.md").read_text()
            + "\nupdate"
        )
        _clear_stale_cache()
        loaded = _load_index(vault)
        assert loaded is None  # stale

    def test_index_build_warns_on_unwritable_dir(self, vault_dir: Path):
        import warnings

        vault = detect(vault_dir)
        # Make .claudron unwritable
        claudron_dir = vault_dir / ".claudron"
        claudron_dir.mkdir(exist_ok=True)
        claudron_dir.chmod(0o444)
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                index = _build_index(vault)
            # Should warn but still return the index
            assert len(w) == 1
            assert "could not write index" in str(w[0].message)
            assert "entries" in index
        finally:
            claudron_dir.chmod(0o755)
