# GitHub reference

Reference that the `github` integration points to. The integration keeps the rules; nothing here is a step.

## Gotcha: reading a piped `gh` call's exit status

Verified live under a fleet-wide 401: two bots independently ran `gh api ... | head` while diagnosing it, all three probes returned rc `0` against a body reading `Bad credentials`, and both first offered those rc values as evidence that auth was fine. Both reported correctly only because they read the response *bodies*. A warning filed under the wrong failure mode is nearly as good as absent — which is why this one is not filed under any mode.

## Gotcha: a 403 blocks REST, not GitHub

In the table, "the rung below" is the integration's section "Gotcha: `permissions` describes the ACCOUNT, not the credential you are holding".

| divergence cause | code | correct response |
|---|---|---|
| one token, different throttle bucket | 403 | expected — reroute and keep working |
| the token may not touch that resource | 403 | not throttling — see the rung below; rerouting cannot help |
| two credentials, one revoked or absent | 401 | a real auth failure — stop and report |

## Gotcha: `permissions` describes the ACCOUNT, not the credential you are holding

Of the integration's statement that a fine-grained PAT can report `admin: true` and still 403 on that repo's secrets:

Measured twice in one night, two different tokens, same repo.

This is the same lie as `permissions.push=true`, which has now been wrong in three distinct
mechanisms on this estate: a credential-helper chain answering with a different identity, two
same-login tokens with different reach, and now a fine-grained scope. One field, three ways to
be wrong, and the field looks authoritative every time.

## Gotcha: a 401 kills every door — there is nothing to reroute to

Of the integration's statement that under a 401 no bucket survives:

Verified live fleet-wide.
