### Fixed — secrets files are written privately and atomically
Fleet and bot `.env` files written by `env migrate --apply` and by composition now go through the same private writer as the bot token: owner-only from the first byte, replaced atomically, and refused when the destination is a symlink or not a regular file.
