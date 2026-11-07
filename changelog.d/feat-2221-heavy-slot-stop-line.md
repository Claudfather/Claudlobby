### Added — heavy-slot.py run stops its job when free memory stays under a stop line set in state/heavy-slot/run-lines, on Linux and macOS (#2221)

With no `run-lines` file a run is exactly as before. With one, the wrapper runs the job in a session of its own and reads the host's free memory every few seconds; once it stays under `stop_mib` for the debounce, SIGTERM goes to the job's process group and to every process found to have left it, SIGKILL follows after a grace, and the run exits 79 with a `heavy_slot_stopped` row. Whether a stop at this line saves a host is not measured.
