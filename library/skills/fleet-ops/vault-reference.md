# Vault reference

Reference that the `shared-documentation-vault` protocol points to. The protocol keeps the rules; nothing here is a step.

## Why `INDEX.md` is off limits

This is the one rule that distinguishes a vault-wired fleet from a raw-tree one, and it is not a style preference: the fleet's `shared/` tree is *inside* `$CLAUDRON_VAULT_PATH`, so those INDEX files are vault files. Opening them by hand is the thing the vault door exists to replace, and it silently misses everything in the vault's other tiers.

## Why queries stay short (Claudron #143)

The brevity is not style: **`recall` ranks correctly on short queries and collapses on prose** (Claudron #143 — score saturates, and the result set becomes a function of the vault rather than of your query). Measured, same door, same subject, only length differing: a three-word query returned the exact note first; a one-sentence description of the *same idea* returned five notes from an unrelated domain.

Under #143 what comes back is a topically self-consistent cluster from an adjacent domain — it reads exactly like a real relevance-ranked answer, which is why it gets accepted instead of retried. Obvious garbage would be safer.

## When #143 is fixed

In the note below, "this section" and "both rungs above" mean the protocol's rules on short queries and on retrying shorter.

> **This section is a MITIGATION, not the fix.** The defect is Claudron #143; this protocol only points
> people at the door. Written assuming #143 is **unfixed**. If it lands such that prose ranks correctly,
> revisit both rungs above rather than leaving them to rot — the short-query rule stays harmless, but the
> reasoning attached to it would no longer be true.

## Why capture, not hand-placing

The capture verb types and dedups the note and puts it in the right tier. Hand-creating a file in the tree bypasses both and is how near-duplicate notes accumulate.
