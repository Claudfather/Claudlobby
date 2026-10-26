"""Claudron path conformance gate (#2520, item 1).

``claudlobby.claudron_refs`` finds every Claudron path or URL this repo names and resolves each
one against a Claudron checkout at the revision recorded in
``claudlobby/contracts/claudron-docs.ref``. These tests prove, offline and on synthetic trees,
that a reference to a path absent at that revision fails the gate and one that exists passes it.
The real checkout is the ``claudron-refs-gate`` CI job's.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from claudlobby import claudron_refs as cr
from claudlobby.claudron_compat import CLAUDRON_INTEGRATION_URL

ROOT = Path(__file__).resolve().parent.parent
SHA = "b8ff418aa1e683499065de9e3e6ee92643d4869e"
OTHER_SHA = "6ca2b948d6e765f6697e1540358ec10a3732a4c1"
POINTER = "documentation/decisions/005-claudfather-boundary-separation.md"

# The files of a Claudron checkout that the text below names.
CLAUDRON = frozenset(
    {
        "CLAUDE.md",
        "SCHEMA.md",
        "VAULT-STRUCTURE.md",
        "documentation/guides/INTEGRATION.md",
        "documentation/specs/CLI_CONTRACT.md",
        "examples/vault/_shared/CONVENTIONS.md",
        POINTER,
    }
)
# The files of this repo that the text below may name beside Claudron's.
LOCAL = frozenset(
    {
        "CLAUDE.md",
        "documentation/integrations/claudron-integration.md",
        "documentation/specs/local-spec.md",
    }
)


def found(text, file="doc.md", local=LOCAL, claudron=CLAUDRON):
    refs = cr.find_references({file: text}, local, claudron)
    return [(r.kind, r.path, r.line) for r in refs]


class TestRefFile:
    def test_a_full_sha_is_read(self, tmp_path):
        ref = tmp_path / "claudron-docs.ref"
        ref.write_text(SHA + "\n")
        assert cr.read_docs_ref(ref) == SHA

    def test_a_release_tag_is_read(self, tmp_path):
        # Once a Claudron release carries the layout, the ref is that tag.
        ref = tmp_path / "claudron-docs.ref"
        ref.write_text("  v0.11.0 \n")
        assert cr.read_docs_ref(ref) == "v0.11.0"

    @pytest.mark.parametrize(
        "text",
        [
            "main",
            "master",
            "HEAD",
            "origin/main",
            "feature/docs-move",
            "b8ff418",
            SHA.upper(),
            "v0.9",
            "",
            "\n",
            SHA + " " + SHA,
            SHA + "\n" + SHA,
        ],
        ids=[
            "main",
            "master",
            "head",
            "remote-branch",
            "branch",
            "short-sha",
            "upper-case-sha",
            "partial-tag",
            "empty",
            "blank-line",
            "two-on-a-line",
            "two-lines",
        ],
    )
    def test_anything_that_moves_or_is_not_a_single_ref_is_refused(self, tmp_path, text):
        ref = tmp_path / "claudron-docs.ref"
        ref.write_text(text)
        with pytest.raises(cr.RefFileError, match="full commit sha"):
            cr.read_docs_ref(ref)

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(cr.RefFileError, match="claudron-docs.ref"):
            cr.read_docs_ref(tmp_path / "claudron-docs.ref")

    def test_the_committed_ref_is_valid(self):
        ref = cr.read_docs_ref()
        assert len(ref) == 40 or ref.startswith("v")


class TestUrls:
    @pytest.mark.parametrize(
        ("text", "path", "ref"),
        [
            (
                "[guide](https://github.com/Claudfather/Claudron/blob/main/documentation/guides/INTEGRATION.md)",
                "documentation/guides/INTEGRATION.md",
                "main",
            ),
            (
                "https://github.com/Claudfather/Claudron/blob/main/documentation/specs/CLI_CONTRACT.md#session-loop-protocol",
                "documentation/specs/CLI_CONTRACT.md",
                "main",
            ),
            (
                "See https://github.com/Claudfather/Claudron/blob/main/SCHEMA.md.",
                "SCHEMA.md",
                "main",
            ),
            (
                "https://github.com/Claudfather/Claudron/tree/main/documentation/guides",
                "documentation/guides",
                "main",
            ),
            (
                "https://raw.githubusercontent.com/Claudfather/Claudron/main/SCHEMA.md",
                "SCHEMA.md",
                "main",
            ),
            (
                'URL = "https://github.com/Claudfather/Claudron/blob/main/SCHEMA.md"',
                "SCHEMA.md",
                "main",
            ),
            (f"https://github.com/Claudfather/Claudron/blob/{SHA}/SCHEMA.md", "SCHEMA.md", SHA),
        ],
        ids=["link", "anchor", "sentence-end", "directory", "raw", "quoted", "sha"],
    )
    def test_a_url_into_the_repo_is_a_reference_to_its_path(self, text, path, ref):
        (r,) = cr.find_references({"doc.md": text}, LOCAL, CLAUDRON)
        assert (r.kind, r.path, r.ref, r.line) == ("url", path, ref, 1)

    @pytest.mark.parametrize(
        "text",
        [
            "https://github.com/Claudfather/Claudron/pull/229",
            "https://github.com/Claudfather/Claudron/issues/12",
            "https://github.com/Claudfather/Claudron/commit/abc1234",
            "https://github.com/Claudfather/Claudron",
            "[Claudron](https://github.com/Claudfather/Claudron)",
            "git+https://github.com/Claudfather/Claudron.git@v0.9.0",
            "https://github.com/someone-else/Claudron/blob/main/SCHEMA.md",
        ],
        ids=["pull", "issue", "commit", "bare", "link", "pin", "other-owner"],
    )
    def test_a_url_that_names_no_file_of_the_tree_is_not_a_reference(self, text):
        assert found(text) == []

    def test_a_url_is_one_reference_not_two(self):
        # The marker rule must not read the "Claudron/blob/..." inside a URL as a path.
        text = (
            "https://github.com/Claudfather/Claudron/blob/main/documentation/guides/INTEGRATION.md"
        )
        assert found(text) == [("url", "documentation/guides/INTEGRATION.md", 1)]


class TestSpellings:
    @pytest.mark.parametrize(
        ("text", "path"),
        [
            (
                "(Claudron `documentation/specs/CLI_CONTRACT.md` §Environment)",
                "documentation/specs/CLI_CONTRACT.md",
            ),
            (
                "Claudron documentation/specs/CLI_CONTRACT.md §capture",
                "documentation/specs/CLI_CONTRACT.md",
            ),
            ("Claudron ``VAULT-STRUCTURE.md``, Claudron #183", "VAULT-STRUCTURE.md"),
            (f"Full algorithm: Claudron repo, `{POINTER}` §10.3.", POINTER),
            (f"(`Claudron:{POINTER}`)", POINTER),
            ("`Claudron/SCHEMA.md:76-112`", "SCHEMA.md"),
            ("Claudron's `SCHEMA.md` (the schema SSOT)", "SCHEMA.md"),
            (
                "the set Claudron owns in `VAULT-STRUCTURE.md §Directory contract`",
                "VAULT-STRUCTURE.md",
            ),
            ("Schema SSOT: `SCHEMA.md` at the Claudron repo root (their E1)", "SCHEMA.md"),
            ("Claudron documentation/guides/NOT-THERE.md", "documentation/guides/NOT-THERE.md"),
        ],
        ids=[
            "backticks",
            "plain",
            "rst",
            "repo-comma",
            "colon",
            "slash",
            "possessive",
            "owns-in",
            "root-after",
            "unknown-name",
        ],
    )
    def test_each_way_this_repo_writes_a_claudron_path_is_read(self, text, path):
        assert [p for _, p, _ in found(text)] == [path]

    @pytest.mark.parametrize(
        ("text", "line"),
        [
            ("Full algorithm: Claudron repo,\n  `documentation/guides/NOT-THERE.md` §10.3.", 2),
            ("# Claudron\n# documentation/guides/NOT-THERE.md", 2),
            ('"""See Claudron\n    documentation/guides/NOT-THERE.md', 2),
            ("one\ntwo Claudron `documentation/guides/NOT-THERE.md`", 2),
        ],
        ids=["wrapped", "comment", "docstring", "later-line"],
    )
    def test_a_path_on_the_line_after_the_marker_is_read_at_its_own_line(self, text, line):
        ((_, _, got),) = found(text)
        assert got == line

    @pytest.mark.parametrize(
        "text",
        [
            "the claudron documentation/guides/NOT-THERE.md",
            "Claudron owns the schema. documentation/guides/NOT-THERE.md is another file",
            "Claudron\n\ndocumentation/guides/NOT-THERE.md",
            "Claudron owns the vault; see documentation/guides/NOT-THERE.md",
            "Claudron #231 moved documentation/guides/NOT-THERE.md",
            "projects/Claudron/documentation/guides/NOT-THERE.md",
            "Claudron [x](../specs/NOT-THERE.md)",
            "Claudron ../specs/NOT-THERE.md",
        ],
        ids=[
            "lower-case",
            "sentence-end",
            "paragraph-break",
            "another-clause",
            "issue-number",
            "inside-a-path",
            "relative-link",
            "relative-plain",
        ],
    )
    def test_a_path_that_does_not_follow_the_name_is_not_read(self, text):
        assert found(text) == []

    def test_a_path_this_repo_has_is_its_own(self):
        text = (
            "Claudron `documentation/integrations/claudron-integration.md` and "
            "Claudron `documentation/specs/local-spec.md`"
        )
        assert found(text) == []
        # ... from a file in another folder too: the path is read from the repo root.
        assert found(text, file="claudlobby/CLAUDE.md") == []

    def test_a_path_relative_to_the_file_that_names_it_is_its_own(self):
        text = "Claudron `specs/local-spec.md`"
        assert found(text, file="documentation/guide.md") == []
        assert [p for _, p, _ in found(text, file="other/guide.md")] == ["specs/local-spec.md"]


class TestNames:
    @pytest.mark.parametrize(
        "token",
        [
            "docs/CLI_CONTRACT.md",
            "documentation/guides/INTEGRATION.md",
            "documentation/specs/CLI_CONTRACT.md",
        ],
    )
    def test_a_docs_path_with_a_name_only_claudron_has_is_read_without_the_marker(self, token):
        assert found(f"see `{token}` for the contract") == [("name", token, 1)]

    @pytest.mark.parametrize(
        "text",
        [
            "see CLI_CONTRACT.md",
            "see `docs/CLAUDE.md`",
            "see `docs/UNKNOWN-NAME.md`",
            'cat "$VAULT/_shared/CONVENTIONS.md"',
            "see `../documentation/specs/CLI_CONTRACT.md`",
            "see `tests/CLI_CONTRACT.md`",
        ],
        ids=["bare", "local-name", "unknown-name", "vault-path", "relative", "other-folder"],
    )
    def test_anything_else_is_not_read_without_the_marker(self, text):
        assert found(text) == []

    def test_a_reference_is_reported_once_per_line(self):
        assert (
            len(found("Claudron `docs/CLI_CONTRACT.md` and Claudron `docs/CLI_CONTRACT.md`")) == 1
        )
        assert len(found("Claudron `docs/CLI_CONTRACT.md`\nClaudron `docs/CLI_CONTRACT.md`")) == 2


class TestResolve:
    @staticmethod
    def ref(path, kind="marker", url_ref=None):
        return cr.Reference("doc.md", 1, kind, path, url_ref)

    def test_a_file_and_a_folder_of_the_tree_resolve(self):
        refs = [self.ref("documentation/guides/INTEGRATION.md"), self.ref("documentation/guides")]
        res = cr.resolve(refs, CLAUDRON, SHA)
        assert res.ok == refs and res.stale == []

    @pytest.mark.parametrize(
        "path",
        [
            "docs/INTEGRATION.md",
            "documentation/guides/integration.md",
            "documentation/guides/NOT-THERE.md",
            "guides/INTEGRATION.md",
            "NOPE.md",
        ],
    )
    def test_a_path_absent_from_the_tree_is_stale(self, path):
        res = cr.resolve([self.ref(path)], CLAUDRON, SHA)
        assert [r.path for r in res.stale] == [path] and res.ok == []

    def test_a_bare_name_follows_its_file_wherever_it_moved(self):
        res = cr.resolve([self.ref("CLI_CONTRACT.md")], CLAUDRON, SHA)
        assert [r.path for r in res.ok] == ["CLI_CONTRACT.md"]

    def test_a_url_naming_main_resolves_at_the_ref_and_is_flagged_as_moving(self):
        good = self.ref("SCHEMA.md", "url", "main")
        gone = self.ref("docs/INTEGRATION.md", "url", "main")
        res = cr.resolve([good, gone], CLAUDRON, SHA)
        assert res.ok == [good] and res.moving == [good] and res.stale == [gone]

    @pytest.mark.parametrize("name", ["master", "HEAD"])
    def test_every_branch_a_url_can_name_for_the_default_branch_moves(self, name):
        good = self.ref("SCHEMA.md", "url", name)
        assert cr.resolve([good], CLAUDRON, SHA).moving == [good]

    @pytest.mark.parametrize("url_ref", [SHA, SHA[:7], SHA[:12]])
    def test_a_url_naming_the_ref_resolves_and_does_not_move(self, url_ref):
        good = self.ref("SCHEMA.md", "url", url_ref)
        res = cr.resolve([good], CLAUDRON, SHA)
        assert res.ok == [good] and res.moving == []
        gone = self.ref("docs/INTEGRATION.md", "url", url_ref)
        assert cr.resolve([gone], CLAUDRON, SHA).stale == [gone]

    @pytest.mark.parametrize("url_ref", [OTHER_SHA, "v0.9.0", SHA[:6], "feature/x"])
    def test_a_url_naming_another_revision_is_not_checked(self, url_ref):
        url = self.ref("docs/INTEGRATION.md", "url", url_ref)
        res = cr.resolve([url], CLAUDRON, SHA)
        assert res.elsewhere == [url] and res.ok == [] and res.stale == []

    def test_a_tag_ref_matches_a_url_naming_that_tag(self):
        url = self.ref("SCHEMA.md", "url", "v0.11.0")
        assert cr.resolve([url], CLAUDRON, "v0.11.0").ok == [url]


def _env():
    """A child env for git, built from nothing: no ambient git config, no prompts, a fixed identity."""
    return {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }


def _git(cwd, *args):
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=_env()
    )
    return done.stdout.strip()


def _write(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _repo(path, files, *, track=True):
    """A git work tree holding ``files``; with ``track`` they are staged (``git ls-files`` sees them)."""
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _write(path, files)
    if track:
        _git(path, "add", "-A")
    return path


def _claudron(path, files, tag=None):
    """A committed Claudron stand-in; returns its directory and its commit sha."""
    _repo(path, files)
    _git(path, "commit", "-q", "-m", "claudron")
    if tag:
        _git(path, "tag", tag)
    return path, _git(path, "rev-parse", "HEAD")


CLAUDRON_FILES = {name: "# doc\n" for name in CLAUDRON}


class TestCheck:
    """The two controls: the same text passes where the path exists and fails where it does not."""

    POINTER_TEXT = f"Full algorithm: Claudron repo,\n`{POINTER}` §10.3.\n"

    def test_a_reference_to_a_path_that_exists_at_the_ref_passes(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        root = _repo(tmp_path / "repo", {"CLAUDE.md": self.POINTER_TEXT})
        code, report = cr.check(root, claudron, sha)
        assert code == 0, report
        assert report.startswith(
            f"OK claudron-refs: 1 reference in 1 file, all found at Claudron {sha[:12]}"
        )

    def test_a_reference_to_a_path_absent_at_the_ref_fails_and_names_it(self, tmp_path):
        files = {k: v for k, v in CLAUDRON_FILES.items() if k != POINTER}
        claudron, sha = _claudron(tmp_path / "claudron", files)
        root = _repo(tmp_path / "repo", {"CLAUDE.md": self.POINTER_TEXT})
        code, report = cr.check(root, claudron, sha)
        assert code == 1
        assert report.startswith(
            f"STALE claudron-refs: 1 of 1 reference not found at Claudron {sha[:12]}"
        )
        assert f"CLAUDE.md:2: {POINTER} (marker)" in report

    def test_the_old_layout_fails_and_the_new_layout_passes(self, tmp_path):
        # The 2026-10-25 move in miniature: one tree names the file where it was, one where it is.
        old = {
            k: v for k, v in CLAUDRON_FILES.items() if k != "documentation/guides/INTEGRATION.md"
        }
        old["docs/INTEGRATION.md"] = "# doc\n"
        before, sha_before = _claudron(tmp_path / "before", old)
        after, sha_after = _claudron(tmp_path / "after", CLAUDRON_FILES)
        text = "Claudron `documentation/guides/INTEGRATION.md` is the front door\n"
        root = _repo(tmp_path / "repo", {"README.md": text})
        code, report = cr.check(root, before, sha_before)
        assert code == 1 and report.startswith("STALE"), report
        assert "README.md:1: documentation/guides/INTEGRATION.md (marker)" in report
        assert cr.check(root, after, sha_after)[0] == 0

    def test_a_gate_that_reads_nothing_fails(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        root = _repo(tmp_path / "repo", {"README.md": "Nothing about the sibling here.\n"})
        code, report = cr.check(root, claudron, sha)
        assert code == 1
        assert "found no Claudron references" in report

    def test_dated_and_log_files_are_skipped_and_counted(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        stale = "Claudron `docs/CLI_CONTRACT.md` as it was\n"
        root = _repo(
            tmp_path / "repo",
            {
                "README.md": "Claudron `SCHEMA.md`\n",
                "CHANGELOG.md": stale,
                "documentation/plans/p.md": stale,
                "documentation/decisions/2026-07-18-x.md": stale,
                "tests/fixtures/snapshot/README.md": stale,
            },
        )
        code, report = cr.check(root, claudron, sha)
        assert code == 0, report
        for counted in ("changelog 1", "plan 1", "dated 1", "fixture 1"):
            assert counted in report

    def test_a_url_naming_main_passes_with_a_note(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        url = "https://github.com/Claudfather/Claudron/blob/main/SCHEMA.md"
        root = _repo(tmp_path / "repo", {"README.md": f"[schema]({url})\n"})
        code, report = cr.check(root, claudron, sha)
        assert code == 0, report
        assert "NOTE: 1 URL names Claudron main, which moves" in report
        assert "README.md:1: SCHEMA.md" in report

    def test_a_url_naming_another_revision_is_listed_not_failed(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        url = f"https://github.com/Claudfather/Claudron/blob/{OTHER_SHA}/docs/INTEGRATION.md"
        root = _repo(tmp_path / "repo", {"README.md": f"{url}\nClaudron `SCHEMA.md`\n"})
        code, report = cr.check(root, claudron, sha)
        assert code == 0, report
        assert "NOTE: 1 URL names another revision and was not checked" in report

    def test_two_urls_are_noted_in_the_plural(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        main = "https://github.com/Claudfather/Claudron/blob/main/SCHEMA.md"
        elsewhere = f"https://github.com/Claudfather/Claudron/blob/{OTHER_SHA}/docs/INTEGRATION.md"
        text = f"{main}\n{main}#a\n{elsewhere}\n{elsewhere}\n"
        root = _repo(tmp_path / "repo", {"README.md": text})
        code, report = cr.check(root, claudron, sha)
        assert code == 0, report
        assert "NOTE: 2 URLs name Claudron main, which move" in report
        assert "NOTE: 2 URLs name another revision and were not checked" in report

    def test_an_untracked_file_is_not_read(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        root = _repo(tmp_path / "repo", {"README.md": "Claudron `SCHEMA.md`\n"})
        _write(root, {"scratch.md": "Claudron `docs/CLI_CONTRACT.md`\n"})
        assert cr.check(root, claudron, sha)[0] == 0

    def test_without_git_every_file_is_read_except_the_checkouts_beside_it(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        root = tmp_path / "export"
        _write(
            root,
            {
                "README.md": "Claudron `SCHEMA.md`\n",
                ".claudron-checkout/x.md": "Claudron `docs/CLI_CONTRACT.md`\n",
                ".git/x.md": "Claudron `docs/CLI_CONTRACT.md`\n",
                "node_modules/x.md": "Claudron `docs/CLI_CONTRACT.md`\n",
            },
        )
        assert cr.check(root, claudron, sha)[0] == 0
        _write(root, {"stale.md": "Claudron `docs/CLI_CONTRACT.md`\n"})
        code, report = cr.check(root, claudron, sha)
        assert code == 1 and "stale.md:1: docs/CLI_CONTRACT.md" in report, report

    def test_a_folder_inside_another_repo_is_read_by_walking_not_by_the_outer_index(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        outer = _repo(tmp_path / "outer", {"keep.txt": "x\n"})
        root = outer / "export"
        _write(root, {"stale.md": "Claudron `docs/CLI_CONTRACT.md`\n"})
        code, report = cr.check(root, claudron, sha)
        assert code == 1 and "stale.md:1: docs/CLI_CONTRACT.md" in report, report

    def test_a_file_that_is_not_text_is_counted_not_fatal(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        root = _repo(tmp_path / "repo", {"README.md": "Claudron `SCHEMA.md`\n"})
        (root / "blob.bin").write_bytes(b"\xff\xfe\x00Claudron")
        _git(root, "add", "-A")
        code, report = cr.check(root, claudron, sha)
        assert code == 0, report
        assert "not text 1" in report

    def test_an_absent_checkout_fails_and_says_so(self, tmp_path):
        root = _repo(tmp_path / "repo", {"README.md": "Claudron `docs/CLI_CONTRACT.md`\n"})
        code, report = cr.check(root, tmp_path / "no-such-dir", SHA)
        assert code == 2
        assert report.startswith("FAIL claudron-refs:") and "absent" in report

    def test_a_checkout_at_another_revision_is_refused(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        root = _repo(tmp_path / "repo", {"README.md": "Claudron `SCHEMA.md`\n"})
        code, report = cr.check(root, claudron, OTHER_SHA)
        assert code == 2
        assert sha[:12] in report and OTHER_SHA[:12] in report

    def test_a_tag_ref_is_verified_against_the_tag(self, tmp_path):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES, tag="v0.11.0")
        root = _repo(tmp_path / "repo", {"README.md": "Claudron `SCHEMA.md`\n"})
        assert cr.check(root, claudron, "v0.11.0")[0] == 0
        assert cr.check(root, claudron, "v0.12.0")[0] == 2

    def test_an_export_without_git_cannot_be_verified_and_says_so(self, tmp_path):
        export = tmp_path / "export"
        _write(export, CLAUDRON_FILES)
        root = _repo(tmp_path / "repo", {"README.md": "Claudron `SCHEMA.md`\n"})
        code, report = cr.check(root, export, SHA)
        assert code == 0, report
        assert "NOTE: the checkout has no .git, so its revision is not verified" in report


class TestCli:
    def test_print_ref_prints_the_committed_ref(self, capsys):
        assert cr.main(["--print-ref"]) == 0
        assert capsys.readouterr().out.strip() == cr.read_docs_ref()

    def test_no_arguments_is_a_usage_error(self, capsys):
        assert cr.main([]) == 2
        assert "usage" in capsys.readouterr().err

    def test_an_absent_checkout_exits_two(self, tmp_path, capsys):
        root = _repo(tmp_path / "repo", {"README.md": "Claudron `SCHEMA.md`\n"})
        assert cr.main(["--claudron", str(tmp_path / "nope"), "--root", str(root)]) == 2
        out = capsys.readouterr().out
        assert "FAIL claudron-refs" in out and "SKIP" not in out

    def test_a_stale_reference_exits_one(self, tmp_path, capsys):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        root = _repo(tmp_path / "repo", {"README.md": "Claudron `docs/CLI_CONTRACT.md`\n"})
        ref = tmp_path / "claudron-docs.ref"
        ref.write_text(sha + "\n")
        assert (
            cr.main(["--claudron", str(claudron), "--root", str(root), "--ref-file", str(ref)]) == 1
        )
        assert "STALE" in capsys.readouterr().out

    def test_a_ref_that_moves_is_refused_before_anything_is_read(self, tmp_path, capsys):
        claudron, _ = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        root = _repo(tmp_path / "repo", {"README.md": "Claudron `SCHEMA.md`\n"})
        ref = tmp_path / "claudron-docs.ref"
        ref.write_text("main\n")
        argv = ["--claudron", str(claudron), "--root", str(root), "--ref-file", str(ref)]
        assert cr.main(argv) == 2
        assert "full commit sha" in capsys.readouterr().err

    def test_list_prints_every_reference(self, tmp_path, capsys):
        claudron, sha = _claudron(tmp_path / "claudron", CLAUDRON_FILES)
        root = _repo(
            tmp_path / "repo",
            {"README.md": "Claudron `SCHEMA.md`\nClaudron `VAULT-STRUCTURE.md`\n"},
        )
        ref = tmp_path / "claudron-docs.ref"
        ref.write_text(sha + "\n")
        argv = ["--claudron", str(claudron), "--root", str(root), "--ref-file", str(ref), "--list"]
        assert cr.main(argv) == 0
        out = capsys.readouterr().out
        assert (
            "README.md:1: SCHEMA.md (marker)" in out
            and "README.md:2: VAULT-STRUCTURE.md (marker)" in out
        )


class TestScan:
    @pytest.mark.parametrize(
        ("path", "rule"),
        [
            ("documentation/plans/2026-10-04-x.md", "plan"),
            ("documentation/plans/archive/y.md", "plan"),
            ("documentation/decisions/2026-07-18-claudron-consumption-door.md", "dated"),
            ("documentation/architecture/system-map-2026-07-30.md", "dated"),
            ("documentation/baselines/naked-bot-2026-09-30.json", "dated"),
            ("CHANGELOG.md", "changelog"),
            ("tests/fixtures/claudron_v090/README.md", "fixture"),
            (".test_durations", "data"),
            ("claudlobby/claudron_refs.py", "own"),
            ("tests/test_claudron_refs.py", "own"),
            ("README.md", None),
            ("CLAUDE.md", None),
            ("claudlobby/claudron_compat.py", None),
            ("documentation/decisions/index-md-convention.md", None),
            ("documentation/plans.md", None),
            ("documentation/runbooks/plans/x.md", None),
        ],
    )
    def test_dated_records_and_logs_are_skipped_the_rest_is_read(self, path, rule):
        assert cr.skipped(path) == rule


@pytest.fixture(scope="module")
def refs():
    scan = cr.scan_repo(ROOT)
    return cr.find_references(scan.files, scan.paths, frozenset())


class TestThisRepo:
    """The gate must be able to see the places it protects."""

    def test_the_integration_url_is_found_in_the_file_that_defines_it(self, refs):
        urls = [r for r in refs if r.file == "claudlobby/claudron_compat.py" and r.kind == "url"]
        assert [CLAUDRON_INTEGRATION_URL.endswith("/" + r.path) for r in urls] == [True]

    def test_the_boundary_spec_pointer_is_found_in_the_instruction_files(self, refs):
        sites = {r.file for r in refs if r.path == POINTER}
        assert {"CLAUDE.md", "AGENTS.md", "claudlobby/CLAUDE.md", "library/CLAUDE.md"} <= sites

    def test_the_scan_skips_dated_records_and_still_reads_the_live_files(self):
        scan = cr.scan_repo(ROOT)
        assert scan.skipped["plan"] > 0 and scan.skipped["dated"] > 0
        assert "documentation/plans/" not in " ".join(scan.files)
        assert "claudlobby/claudron_compat.py" in scan.files


class TestWorkflow:
    """The CI job that runs the gate must fail when it cannot read Claudron, not skip (#2520)."""

    @staticmethod
    def _job():
        workflow = yaml.safe_load((ROOT / ".github/workflows/conformance.yml").read_text())
        return workflow["jobs"]["claudron-refs-gate"]

    def test_a_failed_claudron_fetch_fails_the_job(self):
        job = self._job()
        steps = job["steps"]
        assert "continue-on-error" not in job
        assert not [s.get("name") for s in steps if "continue-on-error" in s]
        assert not [s.get("name") for s in steps if "SKIP" in f"{s.get('name', '')} {s.get('run', '')}"]
        assert not [s.get("name") for s in steps if "if" in s]

    def test_the_check_runs_after_the_one_claudron_checkout(self):
        steps = self._job()["steps"]
        fetch = [i for i, s in enumerate(steps) if s.get("with", {}).get("repository") == "Claudfather/Claudron"]
        check = [i for i, s in enumerate(steps) if "claudlobby.claudron_refs --claudron" in s.get("run", "")]
        assert len(fetch) == 1 and len(check) == 1 and fetch[0] < check[0]
