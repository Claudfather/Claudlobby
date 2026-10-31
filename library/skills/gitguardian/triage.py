"""Evidence words and a class for one GitGuardian secret incident.

`gitguardian_api.sh triage` pipes GET /v1/incidents/secrets/ID?with_occurrences=100
to this script, which prints one line, and never a secret, path, line, commit,
repository or hash:

    triage: incident=ID class=C validity=V placeholder=P ours=O path_class=K occurrences=R/N [note=WHY]

- validity: GitGuardian's own check of the secret.
- placeholder: yes when the secret is a placeholder, no, or cannot_check.
- ours: the name of each variable in this environment whose value is the
  secret, no, or cannot_check.
- path_class: test, fixture, docs or source, for each occurrence read.
- class: live (validity valid, or ours names a variable), false_positive (a
  placeholder that is not valid), or needs_operator (anything else).

Where the secret's bytes come from. The API reference calls the matches of the
occurrence content route character offsets inside the stored content, which
looks like a patch; triage uses only the difference of an occurrence's
indice_start and indice_end, as the secret's length, never its column. An
occurrence's post_line_* (pre_line_* for a deletion) are the file's lines,
counted from 1. So the bytes come from a local checkout holding
the commit (git cat-file): the substring of that length, on those lines, whose
HasMySecretLeaked hash equals the incident's hmsl_hash. With no checkout holding
the commit, or no such substring, placeholder reads cannot_check and note=
says why.

Usage: python3 -I triage.py [CHECKOUT...] < incident.json
       With no CHECKOUT, each git checkout under ./projects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# HasMySecretLeaked's hash, as ggshield computes it (ggshield/verticals/hmsl/
# crypto.py): scrypt over the secret's UTF-8 bytes, salted with
# SHA-256("GitGuardian"). An incident's hmsl_hash is that hash of its secret.
PEPPER = hashlib.sha256(b"GitGuardian").digest()
# At most this many hashes over candidate substrings per triage (about 9 ms
# each on a Raspberry Pi 5), from at most this many occurrences, and no search
# of a matched line longer than this.
MAX_HASHES = 1500
MAX_OCCURRENCES = 10
MAX_LINE = 2000
VALIDITIES = ("valid", "invalid", "failed_to_check", "no_checker", "unknown")
PATH_CLASSES = ("source", "test", "fixture", "docs", "unknown")
# Why placeholder reads cannot_check, the most telling first.
NOTES = (
    "no_hash", "no_scrypt", "no_match", "budget", "long_line", "several_parts",
    "unreadable", "no_location", "no_local_commit", "no_occurrences",
)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SEPARATORS = re.compile(r"[_:./-]+")
_ASCENDING = "1234567890123456789"
_TEST_NAME = re.compile(r"^test_|_test\.|\.test\.|\.spec\.|_spec\.")


class _OutOfHashes(Exception):
    pass


def hmsl(secret: str) -> str:
    return hashlib.scrypt(
        secret.encode("utf-8"), salt=PEPPER, n=2048, r=8, p=1, dklen=32
    ).hex()


def _session_hash(value: str) -> str | None:
    """The hash of a session variable's VALUE, or None for one that cannot be
    encoded: a byte that is not UTF-8 reads as a lone surrogate, and no secret
    GitGuardian read from git holds one."""
    try:
        return hmsl(value)
    except UnicodeEncodeError:
        return None


def is_placeholder(value: str) -> bool:
    """REPLACE_ME, or parts that are each one repeated character or an
    ascending run of digits, once separators and short prefixes are set aside:
    the shapes the repo's PII rule prescribes (ghp_xxxx..., 8888888:AAAA...)."""
    rest = re.sub("(?i)replace_me", "", value)
    parts = [p for p in _SEPARATORS.split(rest) if p]
    if not parts:
        return rest != value
    long_parts = [p for p in parts if len(p) >= 6]
    return bool(long_parts) and all(
        len(set(p)) == 1 or _ASCENDING.startswith(p) for p in long_parts
    )


def path_class(path: object) -> str:
    if not isinstance(path, str) or not path:
        return "unknown"
    *dirs, name = path.lower().split("/")
    if any(d in ("fixture", "fixtures", "testdata", "__fixtures__") for d in dirs):
        return "fixture"
    if any(d in ("test", "tests", "spec", "specs", "__tests__") for d in dirs):
        return "test"
    if _TEST_NAME.search(name):
        return "test"
    if any(d in ("doc", "docs", "documentation") for d in dirs):
        return "docs"
    if name.endswith((".md", ".rst", ".adoc")):
        return "docs"
    return "source"


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *args],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _file_at(checkouts: list[Path], sha: str, rev: str, path: str) -> tuple[str, str | None]:
    """("ok", text) of PATH at REV from the first checkout holding commit SHA,
    or the reason there is none."""
    for checkout in checkouts:
        held = _git(checkout, "cat-file", "-e", f"{sha}^{{commit}}")
        if held is None or held.returncode != 0:
            continue
        blob = _git(checkout, "cat-file", "blob", f"{rev}:{path}")
        if blob is None or blob.returncode != 0 or b"\0" in blob.stdout:
            return "unreadable", None
        try:
            return "ok", blob.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return "unreadable", None
    return "no_local_commit", None


def _find(text: str, first: int, last: int, length: int, target: str, budget: list[int]) -> tuple[str | None, str]:
    """The substring on the file's lines FIRST..LAST whose hash is TARGET and
    whose length in the stored content is LENGTH, or None and the reason."""
    lines = text.split("\n")
    if not 1 <= first <= last <= len(lines):
        return None, "no_match"
    breaks = last - first
    # The stored content looks like a patch, where each line the secret runs on
    # to starts with a +, - or space that the file does not hold.
    width = length - breaks
    head = lines[first - 1]
    if len(head) > MAX_LINE:
        return None, "long_line"
    if width <= 0:
        return None, "no_match"
    span = "\n".join(lines[first - 1:last])
    for start in range(len(head)):
        candidate = span[start:start + width]
        if len(candidate) < width:
            break
        if candidate.count("\n") != breaks:
            continue
        if budget[0] <= 0:
            raise _OutOfHashes
        budget[0] -= 1
        if hmsl(candidate) == target:
            return candidate, "matched"
    return None, "no_match"


def _from_occurrence(occ: dict, checkouts: list[Path], target: str, budget: list[int]) -> tuple[str | None, str]:
    matches = occ.get("matches")
    if not isinstance(matches, list) or not matches:
        return None, "no_location"
    if len(matches) > 1:
        return None, "several_parts"
    match = matches[0] if isinstance(matches[0], dict) else {}
    sha, path = occ.get("sha"), occ.get("filepath")
    deletion = occ.get("change_type") == "deletion"
    side = "pre" if deletion else "post"
    first, last = match.get(f"{side}_line_start"), match.get(f"{side}_line_end")
    start, end = match.get("indice_start"), match.get("indice_end")
    numbers = (first, last, start, end)
    if not (
        isinstance(sha, str) and _SHA.fullmatch(sha)
        and isinstance(path, str) and path
        and all(isinstance(n, int) and not isinstance(n, bool) for n in numbers)
        and end > start
    ):
        return None, "no_location"
    status, text = _file_at(checkouts, sha, f"{sha}^" if deletion else sha, path)
    if text is None:
        return None, status
    return _find(text, first, last, end - start, target, budget)


def _secret(occurrences: list[dict], checkouts: list[Path], target: str) -> tuple[str | None, str | None]:
    """The secret, from the first occurrence whose bytes match the hash, or
    None and the most telling reason none did."""
    budget = [MAX_HASHES]
    whys = []
    for occ in occurrences[:MAX_OCCURRENCES]:
        try:
            found, why = _from_occurrence(occ, checkouts, target, budget)
        except _OutOfHashes:
            whys.append("budget")
            break
        if found is not None:
            return found, None
        whys.append(why)
    if not whys:
        whys.append("no_occurrences")
    return None, min(whys, key=NOTES.index)


def main(argv: list[str]) -> int:
    try:
        return _triage(argv)
    except Exception as exc:  # an exception's message can hold a path or a commit
        print(f"unknown: triage.py stopped on {type(exc).__name__}, and printed nothing it read.")
        return 6


def _triage(argv: list[str]) -> int:
    try:
        incident = json.load(sys.stdin)
    except ValueError:
        incident = None
    if not isinstance(incident, dict) or not isinstance(incident.get("id"), int):
        print("unknown: GitGuardian answered with an incident this script does not read.")
        return 6
    checkouts = [Path(a) for a in argv] or sorted(
        p for p in Path("projects").glob("*") if (p / ".git").exists()
    )
    validity = incident.get("validity")
    if validity not in VALIDITIES:
        validity = "unknown"
    occurrences = [o for o in incident.get("occurrences") or [] if isinstance(o, dict)]
    target = incident.get("hmsl_hash")

    note = None
    if not (isinstance(target, str) and _HEX64.fullmatch(target)):
        note = "no_hash"
    elif not hasattr(hashlib, "scrypt"):
        note = "no_scrypt"
    if note:
        ours = placeholder = "cannot_check"
    else:
        # A placeholder in the environment is not a secret of ours.
        held: dict[str, list[str]] = {}
        for name, value in os.environ.items():
            if value and not is_placeholder(value):
                held.setdefault(value, []).append(name if _NAME.fullmatch(name) else "unnamed")
        names = sorted({n for value, ns in held.items() if _session_hash(value) == target for n in ns})
        several = any(isinstance(o.get("matches"), list) and len(o["matches"]) > 1 for o in occurrences)
        if names:
            ours = ",".join(names)
        elif several or not occurrences:
            ours = "cannot_check"
        else:
            ours = "no"
        secret, note = _secret(occurrences, checkouts, target)
        if secret is None:
            placeholder = "cannot_check"
        else:
            placeholder = "yes" if is_placeholder(secret) else "no"

    if validity == "valid" or ours not in ("no", "cannot_check"):
        verdict = "live"
    elif placeholder == "yes":
        verdict = "false_positive"
    else:
        verdict = "needs_operator"
    kinds = sorted({path_class(o.get("filepath")) for o in occurrences}, key=PATH_CLASSES.index)
    count = incident.get("occurrences_count")
    words = [
        f"incident={incident['id']}",
        f"class={verdict}",
        f"validity={validity}",
        f"placeholder={placeholder}",
        f"ours={ours}",
        f"path_class={','.join(kinds) or 'unknown'}",
        f"occurrences={len(occurrences)}/{count if isinstance(count, int) else '?'}",
    ]
    if placeholder == "cannot_check" and note:
        words.append(f"note={note}")
    print("triage: " + " ".join(words))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
