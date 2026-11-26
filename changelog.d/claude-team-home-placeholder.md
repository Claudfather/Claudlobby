### Changed — the team account's home path passes both home-path checks

`claudlobby` joins the placeholder home names in the content-check and in the boundary test. It is the team account that setup will create on a Linux computer, so its home directory is the product's own path, never a person's. Every other name is still refused. This lands first, on its own, because the content-check reads its rules from the base branch; the setup change that names that home comes after it.
