### Fixed — `sweep` and `sweep-personal` hand an audit its lens first (#2983)

The dispatch step of both skills called the Skill tool with `args` that began with `--auto`, so a headless audit received no lens token, which it requires as the first token of its arguments. Step 3 of each now names the call for an audit row, `skill="claudna:audit"` with `args="<type> --auto --output github <dir>"`, and for the enhancement row, `skill="claudna:product-enhance"`, which takes no lens. It also says that the skill field takes the skill's name alone.
