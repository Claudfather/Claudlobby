### Fixed — distinguish recorder status from readable team summaries

Connected workspaces label recorder-status coverage separately from team-summary
coverage, so independent reads can differ without presenting conflicting global
source counts. Pending recorder reads say they are checking; failed reads retain
their recovery guidance, and recorder down, unknown, and quiet evidence remains visible.
