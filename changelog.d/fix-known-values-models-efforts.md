### Fixed — `effort: xhigh` parses, and the known model names follow the Claude Code model docs

`effort` in `fleet.yaml` is checked against a fixed list when the config is parsed, and a value outside it is refused. The list was `low`, `medium`, `high` and `max`, so `effort: xhigh` was refused, though `claude --effort` takes it. The list is now what `--effort` accepts: `low`, `medium`, `high`, `xhigh` and `max`.

The model names the validator knows now follow code.claude.com/docs/en/model-config, read on 2026-11-20: its aliases (`default`, `best`, `fable`, `opus`, `sonnet`, `haiku` and `opusplan`) and the Anthropic API model IDs it names, plus `claude-haiku-4-5-20251001`, which the repo's harness pins. Any of those names but `default`, with `[1m]` appended, is known too. A model name outside the list still only warns.
