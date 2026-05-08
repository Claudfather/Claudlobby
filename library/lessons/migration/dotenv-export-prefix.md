---
title: "Lesson: hand-rolled .env parsers must strip the `export ` prefix"
---

Hand-edited `.env` files in shell-y systems frequently use the `export VAR=value` form rather than bare `VAR=value`. Both work for `source`-style consumption (POSIX `.` and bash `source` understand both), but a parser that only does `k, v = line.split("=", 1)` will store keys with the literal `"export "` prefix attached.

## The bug

`_load_env` in `claudlobby/__main__.py` had this shape:

```python
# bad
for line in env_file.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    if k and k not in os.environ:
        os.environ[k.strip()] = v.strip().strip('"').strip("'")
```

For an input file containing:

```
export GITHUB_PAT="ghp_abc123..."
```

this set `os.environ["export GITHUB_PAT"] = "ghp_abc123..."` — the prefix landed in the **key**, not stripped. The validator then asked `"GITHUB_PAT" in os.environ` and got `False`, firing a "not set" warning even though the user clearly had it set.

In our migration testing on the Pi this produced **12 simultaneous false-positive warnings** for vars that were correctly populated in `local/<fleet>/.env` — easy to start ignoring, which then masks real signals.

## The fix

Strip the prefix in the key after parsing:

```python
k = k.strip()
if k.startswith("export "):
    k = k[len("export "):].strip()
```

Better, factor it into a small helper used everywhere `.env` files are read:

```python
def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[len("export "):].strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out
```

## A related trap to avoid

`str.lstrip("export ")` is **not** a prefix strip — it's a *char-set* strip. `lstrip` removes any leading combination of `e`, `x`, `p`, `o`, `r`, `t`, ` ` characters until it hits one that isn't in the set. So `"EXPENSE_KEY".lstrip("export ")` returns `"NSE_KEY"` — silently corrupted.

Use `str.removeprefix("export ")` (Python 3.9+), or an explicit `if k.startswith("export "): k = k[len("export "):]`. Don't use `lstrip` for prefixes.

## Why use `.env` at all if it's this fragile?

The `.env` format is the lingua franca for secrets across shells, Docker, systemd `EnvironmentFile=`, Python tools, etc. It's worth the careful parsing because of where the file gets read from outside your tool — operators expect `export VAR="value"` to be valid, and they expect commenting with `#` to work. Match that convention; don't invent your own format.
