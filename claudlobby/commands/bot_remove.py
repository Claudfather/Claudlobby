"""Roster changes await the common Git update owner's supported policy."""
from ..command_result import CommandFailure


def dispatch(args):
    raise CommandFailure("conflict", "bot remove is unavailable: Git roster changes are not yet supported",
        hint="Keep existing bot state and declarations unchanged; use the common Git updater only for supported configuration changes.",
        data={"native_outcome":"unattempted","changed":False})
