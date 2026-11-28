### Fixed — browser Claude sign-in reaches its link with Claude 2.1.325 (#2999)

The private setup-token screen now accepts the terminal output of Claude 2.1.325: cursor save/restore, the scroll-region reset, an exact set of presentation-only private modes, unanswered terminal queries, the US-ASCII G0 designation and shift-in, and OSC 8 links with an `id` parameter. Before this, every connected sign-in at the pinned binary ended `needs_attention`/`unsupported_output` before showing a link.
