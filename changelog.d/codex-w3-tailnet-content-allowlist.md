### Changed — content-check allows three synthetic tailnet IPv4 uses by exact path

The content check's `ip-address` rule flagged the shared tailnet range constant in `claudlobby/plane/owner_ssh.py` and synthetic `100.64.0.x` fixtures in two owner SSH test files. This adds three exact-path entries to the check's allowlist, one per file, each with its reason. It is content-check policy only: no runtime behavior, no source or test changes, and the check's rules are unchanged. The check reads the allowlist from the base branch, so these entries take effect only once merged.
