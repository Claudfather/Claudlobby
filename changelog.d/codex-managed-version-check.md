### Fixed — preserve the result of the managed Mac version check

A provider version check on a managed Mac could report a generic exit error when the last helper process was already exiting. The check now preserves its measured result once the operating system confirms the process group is gone.
