### Fixed — CI test guards follow their runtime contracts (#2923)

The shell parse checks now apply POSIX syntax validation only to preinstall scripts that declare `#!/bin/sh`, while retaining the system Bash parse check for every shipped shell script, including Bash preinstall entrypoints. The Claude update test now checks the output of `fleet_launch_path` in a private HOME with an empty environment, so the guard measures the system-before-user PATH order without depending on a particular number of `printf` statements in the function.
