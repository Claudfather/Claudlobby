### Fixed — retain handoff results and recover early upgrades

Recent handoff checks now use the saved context timestamp as well as the file
write time. Activation retains the native handoff result before checking the
capture, so a known recent-save skip is not lost as an unknown send. Resuming
an unknown handoff still never sends it again.

An early upgrade abort can restore parked bot unit files when no bot stop has
begun. It verifies the original configuration and native disposition, preserves
handoff receipts, and does not stop, start or message a bot. Interrupted file
restoration uses the existing abort and configuration journals.
