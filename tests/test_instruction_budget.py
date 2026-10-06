"""The instruction files stay small enough to be read, and both agents read the same words.

Why this exists. The root CLAUDE.md grew from 67k to 194k characters in eight weeks,
mostly because each change appended its reasoning to its row of the scripts table.
Claude Code loads it whole into every session in this checkout and into every bot
under it (it reads the CLAUDE.md of each parent directory, and bots live under
local/), and warns past 150k characters. Codex reads AGENTS.md within ONE 32 KiB
budget for the whole chain from the repo root down to the folder it starts in,
measured against its own loader ("project doc exceeds remaining budget;
truncating"). A 165k copy lost 80% of its text that way, including the secrets and
PII rules. A warning in a UI did not stop the growth; a failing test at PR time does.

What it pins:

- the chain Codex reads to reach each folder's rules fits in 32 KiB: every
  CLAUDE.md from the root down, the last one up to its `## Script reference`;
- every nested instruction file stays under Claude Code's per-file warning, and the
  failure names the rows to move into their scripts' headers;
- the root indexes every runtime script and harness/CLAUDE.md every harness script,
  once each, one short line each, with nothing but entries and group headers;
- every CLAUDE.md has an AGENTS.md beside it that is a byte-for-byte copy, and
  every .claude/skills/<name>/ a byte-for-byte copy at .agents/skills/<name>/.
  Copies, not symlinks: tests/prepare_resources.py refuses a symlink in the git
  index, and Codex's skill loader (app-server `skills/list`, measured) skips a
  real directory holding a symlinked SKILL.md file without an error.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent
ROOT_MD = REPO_DIR / "CLAUDE.md"

# Codex's default `project_doc_max_bytes`, read from the installed binary's default
# config; one budget shared along the AGENTS.md chain (measured).
CODEX_PROJECT_DOC_MAX_BYTES = 32 * 1024
# Claude Code warns once an instruction file passes this many characters.
CLAUDE_CODE_FILE_WARN_CHARS = 150_000
# An index line names a script and says what it is; its history lives elsewhere.
INDEX_LINE_MAX_CHARS = 200
REFERENCE_HEADING = "## Script reference"
INSTRUCTION_NAMES = ("CLAUDE.md", "AGENTS.md")
INDEX_ENTRY = re.compile(r"^- `([^`]+)` — (.+)$")
GROUP_HEADER = re.compile(r"^\*\*[^*]+\*\*$")
TABLE_ROW = re.compile(r"^\| `([^`]+)` \| (.*) \|$", re.M)

# (file holding the index, the heading that opens it, the folder it indexes)
INDEXES = [
    (
        ROOT_MD,
        "### Runtime scripts (`claudlobby/_runtime_scripts/`)",
        "claudlobby/_runtime_scripts",
    ),
    (REPO_DIR / "harness" / "CLAUDE.md", "## Index", "harness"),
]


def _tracked(*pathspecs: str) -> list[str]:
    """Tracked paths matching `pathspecs`. Tracked, never globbed: local/, runtime/
    and state/ hold generated bot CLAUDE.md files that are not this repo's to budget."""
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
    """Every folder holding a tracked CLAUDE.md, the repo root included."""
    return sorted(
        {
            (REPO_DIR / p).parent
            for p in _tracked("CLAUDE.md", "*/CLAUDE.md")
            if not p.startswith("tests/")
        }
    )


def _section_lines(md: Path, heading: str) -> list[str]:
    text = md.read_text(encoding="utf-8")
    assert heading + "\n" in text, (
        f"{md.relative_to(REPO_DIR)} lost its `{heading}` section"
    )
    body = text.split(heading + "\n", 1)[1]
    return re.split(r"^#{1,3} ", body, maxsplit=1, flags=re.M)[0].splitlines()


def _entries(md: Path, heading: str) -> list[tuple[str, str]]:
    return [
        (m.group(1), line)
        for line in _section_lines(md, heading)
        for m in [INDEX_ENTRY.match(line)]
        if m
    ]


def _scripts(folder: str) -> list[str]:
    prefix = folder + "/"
    names = [p[len(prefix) :] for p in _tracked(prefix) if "/" not in p[len(prefix) :]]
    return sorted(n for n in names if n not in INSTRUCTION_NAMES)


def _head_bytes(md: Path) -> int:
    """Bytes Codex must read to reach this file's rules: all of it, or up to its reference."""
    raw = md.read_bytes()
    i = raw.find(("\n" + REFERENCE_HEADING + "\n").encode())
    return len(raw) if i < 0 else i + 1


def test_instruction_dirs_are_found():
    """Non-vacuous: every check below is empty-handed if discovery breaks."""
    found = {d.relative_to(REPO_DIR).as_posix() for d in _instruction_dirs()}
    assert {".", "claudlobby", "claudlobby/_runtime_scripts", "harness"} <= found, found


def test_codex_chain_reaches_every_folders_rules():
    over = []
    for d in _instruction_dirs():
        chain = [a for a in (d, *d.parents) if a == REPO_DIR or REPO_DIR in a.parents]
        chain = [a for a in reversed(chain) if (a / "CLAUDE.md").is_file()]
        sizes = [len((a / "CLAUDE.md").read_bytes()) for a in chain[:-1]] + [
            _head_bytes(d / "CLAUDE.md")
        ]
        if sum(sizes) > CODEX_PROJECT_DOC_MAX_BYTES:
            parts = " + ".join(
                f"{(a / 'CLAUDE.md').relative_to(REPO_DIR)} {n:,}"
                for a, n in zip(chain, sizes)
            )
            over.append(f"{parts} = {sum(sizes):,}")
    assert not over, (
        f"Codex reads the AGENTS.md chain from the root to its start folder within one "
        f"{CODEX_PROJECT_DOC_MAX_BYTES:,}-byte budget, and these chains outrun it, so a "
        f"session started there loses that folder's rules: {over}. Trim the root first "
        f"(one line per thing; detail goes in the nested file or the script's header)."
    )


def test_nested_instruction_files_stay_under_claude_code_warning():
    over = []
    for d in _instruction_dirs():
        md = d / "CLAUDE.md"
        text = md.read_text(encoding="utf-8")
        if d == REPO_DIR or len(text) <= CLAUDE_CODE_FILE_WARN_CHARS:
            continue
        rows = sorted(
            ((len(desc), name) for name, desc in TABLE_ROW.findall(text)), reverse=True
        )[:5]
        longest = ", ".join(f"{name} ({n:,})" for n, name in rows) or "no table rows"
        over.append(
            f"{md.relative_to(REPO_DIR)} is {len(text):,} chars; longest rows: {longest}"
        )
    assert not over, (
        f"over Claude Code's {CLAUDE_CODE_FILE_WARN_CHARS:,}-character warning: {over}. A nested "
        f"file loads whole the first time a session opens anything in its folder; move those "
        f"rows' history into the scripts' own headers."
    )


@pytest.mark.parametrize(
    "md,heading,folder", INDEXES, ids=lambda v: getattr(v, "name", v)
)
def test_index_names_every_script_exactly_once(md, heading, folder):
    names = [n for n, _ in _entries(md, heading)]
    scripts = _scripts(folder)
    assert scripts, (
        f"found no tracked scripts under {folder}/; the check would pass vacuously"
    )
    missing = [s for s in scripts if s not in names]
    stale = [n for n in names if n not in scripts]
    doubled = sorted({n for n in names if names.count(n) > 1})
    assert not (missing or stale or doubled), (
        f"{md.relative_to(REPO_DIR)} `{heading}` is out of step with {folder}/: missing "
        f"{missing}, naming scripts that do not exist {stale}, listed twice {doubled}. "
        f"Give each script exactly one line: `- `name` — what it is`."
    )


@pytest.mark.parametrize(
    "md,heading,folder", INDEXES, ids=lambda v: getattr(v, "name", v)
)
def test_index_is_one_line_entries_only(md, heading, folder):
    lines = _section_lines(md, heading)
    first = next(
        (
            i
            for i, l in enumerate(lines)
            if GROUP_HEADER.match(l) or INDEX_ENTRY.match(l)
        ),
        len(lines),
    )
    stray = [
        l[:80]
        for l in lines[first:]
        if l.strip() and not GROUP_HEADER.match(l) and not INDEX_ENTRY.match(l)
    ]
    long = {
        name: len(line)
        for name, line in _entries(md, heading)
        if len(line) > INDEX_LINE_MAX_CHARS
    }
    assert not stray and not long, (
        f"{md.relative_to(REPO_DIR)} `{heading}` must hold only one-line entries and **group** "
        f"headers after its intro. Lines that are neither: {stray}. Entries over "
        f"{INDEX_LINE_MAX_CHARS} characters: {long}. Contracts and history belong in the "
        f"script reference or the script's header."
    )


def test_every_agents_md_is_a_copy_of_its_claude_md():
    tracked = [p for p in _tracked("AGENTS.md", "*/AGENTS.md") if not p.startswith("tests/")]
    assert tracked, "no AGENTS.md is tracked; Codex gets no instructions"
    wrong = {}
    for rel in tracked:
        path = REPO_DIR / rel
        claude = path.parent / "CLAUDE.md"
        if path.is_symlink():
            wrong[rel] = "a symlink (tests/prepare_resources.py refuses symlinks)"
        elif not claude.is_file():
            wrong[rel] = "stranded: no CLAUDE.md beside it"
        elif path.read_bytes() != claude.read_bytes():
            wrong[rel] = "differs from the CLAUDE.md beside it"
    assert not wrong, (
        f"{wrong}. Each AGENTS.md is a byte-for-byte copy of the CLAUDE.md beside it, so "
        f"Codex reads what Claude Code reads: edit the CLAUDE.md, then `cp CLAUDE.md AGENTS.md` "
        f"in that folder. Move or delete an AGENTS.md together with its CLAUDE.md."
    )


def test_every_claude_md_has_an_agents_md():
    lacking = [
        (d / "CLAUDE.md").relative_to(REPO_DIR).as_posix()
        for d in _instruction_dirs()
        if not (d / "AGENTS.md").is_file()
    ]
    assert not lacking, (
        f"Codex reads AGENTS.md, and these CLAUDE.md files have no AGENTS.md beside them: "
        f"{lacking}. In each folder: `cp CLAUDE.md AGENTS.md`."
    )


def _tracked_skills(prefix: str) -> dict[str, dict[str, Path]]:
    """{skill: {relative file: path}} for the tracked files under `prefix`."""
    skills: dict[str, dict[str, Path]] = {}
    for p in _tracked(prefix):
        name, _, rel = p[len(prefix):].partition("/")
        if rel:
            skills.setdefault(name, {})[rel] = REPO_DIR / p
    return skills


def test_every_claude_skill_is_mirrored_for_codex():
    """Read from git, like every check here: an untracked .DS_Store or a personal
    skill on one machine is not the repository's to mirror."""
    claude = _tracked_skills(".claude/skills/")
    agents = _tracked_skills(".agents/skills/")
    skills = sorted(name for name, files in claude.items() if "SKILL.md" in files)
    assert skills, "found no tracked .claude/skills/*/SKILL.md; the check would pass vacuously"
    problems = []
    for name in skills:
        copy = agents.get(name)
        if copy is None:
            problems.append(f"{name}: no tracked .agents/skills/{name}/")
        elif any(p.is_symlink() for p in copy.values()):
            problems.append(f"{name}: holds a symlink")
        elif set(copy) != set(claude[name]):
            problems.append(f"{name}: missing {sorted(set(claude[name]) - set(copy))}, "
                            f"extra {sorted(set(copy) - set(claude[name]))}")
        elif any(copy[rel].read_bytes() != claude[name][rel].read_bytes() for rel in copy):
            problems.append(f"{name}: differs from .claude/skills/{name}")
    for name in sorted(set(agents) - set(skills)):
        problems.append(f"{name}: .agents/skills entry with no .claude/skills source")
    assert not problems, (
        f"{problems}. Each Codex skill is a byte-for-byte copy of its Claude source: "
        f"`rm -rf .agents/skills/<name> && cp -R .claude/skills/<name> .agents/skills/<name>`."
    )
