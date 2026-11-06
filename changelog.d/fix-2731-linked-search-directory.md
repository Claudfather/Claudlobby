### Fixed — a new bot or job no longer stops at preparation when a systemd search directory is a symlink (#2731)

On Debian 12 the `systemd` package ships `/etc/xdg/systemd/user` as a link, and systemd lists it. The updater refused every new native unit on such a host, so a first fleet stopped at preparation and an update could add no bot or job. Now only the install directory has to be unredirected. A unit reached through a linked directory still counts as present.
