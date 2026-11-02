### Docs — guide new users through browser-connected first-team setup (#2598)

README, getting-started and `/setup` now lead to the canonical first-team route, keeping Telegram seed activation an explicit operator choice. The shared Python prerequisite checks the supported version, resolves the real executable and runs a created virtual environment before assembly; setup uses an exact supplied bundle path or an explicitly chosen source build instead of searching private directories.
