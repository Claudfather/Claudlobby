"""The instruction files stay small enough to be read, and both agents read the same words.

Why this exists. The root CLAUDE.md grew from 67k to 189k characters in seven weeks,
mostly because each change appended its reasoning to its row of the lib/ table. Past
150k characters Claude Code warns on every session start, and it loads the file whole
into every session in this checkout and into every bot under it (Claude Code reads the
CLAUDE.md of each parent directory, and bots live under local/). Codex reads AGENTS.md
and stops at 32 KiB by default: a 165k copy lost 80% of its text that way, including
the secrets and PII rules. A warning in a UI did not stop the growth; a failing test
at PR time does.

What it pins:

- the root CLAUDE.md fits Codex's default project_doc_max_bytes (32 KiB);
- every other instruction file stays under Claude Code's per-file warning;
- the root's lib/ index names every script in lib/, once, each on one short line;
- every CLAUDE.md has an AGENTS.md beside it that is a symlink to it, and no AGENTS.md
  is anything else, so Claude Code and Codex cannot drift apart;
- every .claude/skills/<name> has a .agents/skills/<name> DIRECTORY symlink to it.
  Measured against Codex's own skill loader (app-server `skills/list`): a symlinked
  skill directory is discovered, while a real directory holding a symlinked SKILL.md
  file is skipped without an error. So the link is the directory, never the file.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent
ROOT_MD = REPO_DIR / "CLAUDE.md"
LIB_DIR = REPO_DIR / "lib"

# Codex reads at most this many bytes of project instructions: its default
# `project_doc_max_bytes`, read from the installed binary's default config.
CODEX_PROJECT_DOC_MAX_BYTES = 32 * 1024
# Claude Code warns once an instruction file passes this many characters
# ("CLAUDE.md is over the 150.0k-char limit").
CLAUDE_CODE_FILE_WARN_CHARS = 150_000
# An index line names a script and says what it is; its history lives elsewhere.
INDEX_LINE_MAX_CHARS = 200

INDEX_HEADING = "### `lib/` scripts"
INDEX_ENTRY = re.compile(r"^- `([^`]+)` — (.+)$")
INSTRUCTION_NAMES = ("CLAUDE.md", "AGENTS.md")


def _tracked(*pathspecs: str) -> list[str]:
    """Tracked paths matching `pathspecs`, relative to the repo root.

    Tracked, never globbed: local/, runtime/ and state/ hold generated bot
    CLAUDE.md files and operator scripts that are not this repo's to budget.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        pytest.skip(f"git ls-files unavailable here: {out.stderr.strip()}")
    return sorted(p for p in out.stdout.split("\0") if p)


def _instruction_dirs() -> list[Path]:
    """Every directory that carries a tracked CLAUDE.md, the repo root included."""
    dirs = {
        (REPO_DIR / p).parent
        for p in _tracked("CLAUDE.md", "*/CLAUDE.md")
        if not p.startswith("tests/")
    }
    return sorted(dirs)


def _lib_index() -> list[tuple[str, str, str]]:
    """(script, description, line) for every entry under the root's lib/ index."""
    text = ROOT_MD.read_text(encoding="utf-8")
    assert INDEX_HEADING in text, f"root CLAUDE.md lost its `{INDEX_HEADING}` section"
    section = text.split(INDEX_HEADING, 1)[1]
    section = re.split(r"^#{1,3} ", section, maxsplit=1, flags=re.M)[0]
    entries = []
    for line in section.splitlines():
        m = INDEX_ENTRY.match(line)
        if m:
            entries.append((m.group(1), m.group(2), line))
    return entries


def _lib_scripts() -> list[str]:
    """Tracked files directly under lib/, minus its own instruction files."""
    names = [p.split("/", 1)[1] for p in _tracked("lib/*") if p.count("/") == 1]
    return sorted(n for n in names if n not in INSTRUCTION_NAMES)


def test_instruction_dirs_are_found():
    """Non-vacuous: every check below is empty-handed if discovery breaks."""
    found = {d.relative_to(REPO_DIR).as_posix() for d in _instruction_dirs()}
    assert {".", "lib", "claudlobby"} <= found, found


def test_root_claude_md_fits_codex_budget():
    size = len(ROOT_MD.read_bytes())
    assert size <= CODEX_PROJECT_DOC_MAX_BYTES, (
        f"root CLAUDE.md is {size:,} bytes; Codex reads the first "
        f"{CODEX_PROJECT_DOC_MAX_BYTES:,} and drops the rest, and Claude Code loads it "
        f"whole into every session and every bot under this checkout. Keep one line per "
        f"thing here: move detail into lib/CLAUDE.md, claudlobby/CLAUDE.md, the script's "
        f"or module's own header, or documentation/."
    )


def test_nested_instruction_files_stay_under_claude_code_warning():
    over = {}
    for d in _instruction_dirs():
        md = d / "CLAUDE.md"
        chars = len(md.read_text(encoding="utf-8"))
        if chars > CLAUDE_CODE_FILE_WARN_CHARS:
            over[md.relative_to(REPO_DIR).as_posix()] = chars
    assert not over, (
        f"over Claude Code's {CLAUDE_CODE_FILE_WARN_CHARS:,}-character warning: {over}. "
        f"A nested file loads whole the first time a session reads anything in its "
        f"folder; move a script's history into that script's own header."
    )


def test_lib_index_names_every_script_exactly_once():
    names = [name for name, _, _ in _lib_index()]
    scripts = _lib_scripts()
    assert scripts, (
        "found no tracked scripts under lib/; the check would pass vacuously"
    )
    missing = [s for s in scripts if s not in names]
    stale = [n for n in names if n not in scripts]
    doubled = sorted({n for n in names if names.count(n) > 1})
    assert not (missing or stale or doubled), (
        f"root CLAUDE.md `{INDEX_HEADING}` index is out of step with lib/: "
        f"missing {missing}, naming scripts that do not exist {stale}, listed twice "
        f"{doubled}. Give each script exactly one line: `- `name` — what it is`."
    )


def test_lib_index_entries_are_one_liners():
    long = {
        name: len(line)
        for name, _, line in _lib_index()
        if len(line) > INDEX_LINE_MAX_CHARS
    }
    assert not long, (
        f"index lines over {INDEX_LINE_MAX_CHARS} characters: {long}. Say what the "
        f"script is for in one line; its contracts and history belong in "
        f"lib/CLAUDE.md or the script's header."
    )


def test_every_agents_md_is_a_symlink_to_its_claude_md():
    tracked = [
        p for p in _tracked("AGENTS.md", "*/AGENTS.md") if not p.startswith("tests/")
    ]
    assert tracked, "no AGENTS.md is tracked; Codex gets no instructions"
    wrong = {}
    for rel in tracked:
        path = REPO_DIR / rel
        if not path.is_symlink():
            wrong[rel] = "a regular file"
        elif os.readlink(path) != "CLAUDE.md":
            wrong[rel] = f"a symlink to {os.readlink(path)!r}"
    assert not wrong, (
        f"{wrong}. Each AGENTS.md must be a symlink to the CLAUDE.md beside it "
        f"(`ln -s CLAUDE.md AGENTS.md`); a copy drifts from what Claude Code reads."
    )


def test_every_claude_md_has_an_agents_md():
    lacking = [
        (d / "CLAUDE.md").relative_to(REPO_DIR).as_posix()
        for d in _instruction_dirs()
        if not (d / "AGENTS.md").is_symlink()
    ]
    assert not lacking, (
        f"Codex reads AGENTS.md, and these CLAUDE.md files have no AGENTS.md symlink "
        f"beside them: {lacking}. In each folder: `ln -s CLAUDE.md AGENTS.md`."
    )


def test_every_claude_skill_is_linked_for_codex():
    claude_skills = REPO_DIR / ".claude" / "skills"
    agents_skills = REPO_DIR / ".agents" / "skills"
    skills = sorted(p.parent.name for p in claude_skills.glob("*/SKILL.md"))
    assert skills, "found no .claude/skills/*/SKILL.md; the check would pass vacuously"
    problems = []
    for name in skills:
        link = agents_skills / name
        if not link.is_symlink():
            problems.append(f"{name}: .agents/skills/{name} is not a symlink")
        elif link.resolve() != (claude_skills / name).resolve():
            problems.append(f"{name}: points at {os.readlink(link)!r}")
    for entry in sorted(agents_skills.iterdir()) if agents_skills.is_dir() else []:
        if entry.name not in skills:
            problems.append(
                f"{entry.name}: .agents/skills entry with no .claude/skills source"
            )
    assert not problems, (
        f"{problems}. Each Codex skill is a directory symlink to its Claude source: "
        f"`ln -s ../../.claude/skills/<name> .agents/skills/<name>`. Not a copy, and "
        f"not a symlinked SKILL.md file, which Codex's loader skips."
    )
