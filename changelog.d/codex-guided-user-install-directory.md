### Fixed — guided setup offers the observed persistent user directory

Guided setup lets Enter select the normal per-user LaunchAgents or systemd user
directory when the native manager reports that path. Linux honors an absolute
XDG_CONFIG_HOME. Other observed paths remain available through an explicit
choice or --install-directory; missing user-path evidence never defaults to a
system, control or transient directory. All local approval gates remain explicit.
