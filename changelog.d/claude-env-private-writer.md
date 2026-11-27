### Fixed — secrets files are written privately and atomically

Fleet and bot `.env` files written by `migration env --apply` (and by the development compositor's scaffolding) now go through the same private writer as the bot token: owner-only from the first byte, replaced atomically, and refused, with nothing written, when a destination is a symlink or not a regular file.
