"""Path-ownership audit — the compositor's guarantee that no emitted file carries
a flat, dangling, or otherwise-improper absolute fleet path.

The generate-time guard (composer) and the freshbox self-containment audit both
call in here, so the definition of "improper" can never drift between the two.
This extends the fresh-box self-containment contract to cover PATHS, the same
shape it already covers for permissions: the compositor *derives* correct,
self-contained wiring rather than trusting hand-written absolute inputs.
"""

from __future__ import annotations

# Path anchors the composer exports into bot.conf. A ${VAR} in an MCP fragment,
# or a $VAR in bot.conf, that names one of these resolves — at runtime for
# .mcp.json, at source time for bot.conf — to a composer-derived, migration-safe
# absolute path. They are the blessed way to express an in-fleet absolute path in
# a compose source, so that a raw absolute fleet path stands out as the
# dangling-path smell the guard rejects.
#   CLAUDLOBBY_ROOT — the install root (paths.root)
#   FLEET_ROOT      — the fleet overlay root (paths.fleet_config_dir)
#   BOT_DIR         — this bot's runtime dir (paths.bot_runtime(bot_id))
COMPOSER_PROVIDED_PATH_ANCHORS: tuple[str, ...] = (
    "CLAUDLOBBY_ROOT",
    "FLEET_ROOT",
    "BOT_DIR",
)
