# DRO Core Correctness and Optimization Design

**Goal:** Fix the confirmed core correctness defects in discovery, AdGuard lookup, benchmark statistics, candidate ranking, lock handling, and rewrite reporting while preserving public API, database schema, and decision policy.

## Scope

The first implementation pass covers only the confirmed defects and regression tests. It does not change thresholds, AND/OR decision policy, failover semantics, curl TLS flags, API schemas, or database columns. Context-manager and benchmark parallelism optimizations remain optional follow-up work and will be attempted only if existing interfaces and focused tests show no contract change.

## Design

- `read_adguard_rewrite` treats zero discovered endpoints and multiple discovered endpoints as distinct unavailable states, logs a different warning for each, and never mutates AdGuard.
- `calculate_statistics` keeps the existing health rule and returns `None` for jitter with zero valid samples, `0.0` for one valid sample, and population standard deviation for two or more samples.
- `rank_candidates` uses a total sort key so healthy candidates with missing median or jitter remain sortable without changing the ordering of fully measured candidates.
- Decision locking normalizes the supplied manual lock by whitespace presence only: `None`, empty, and whitespace-only values do not lock; a non-empty IP continues to lock. Existing model validation remains authoritative for API/database values.
- The optimizer records `current_rewrite_in_public_dns` before adding the current rewrite to benchmark candidates. The existing `current_rewrite_included` field continues to describe candidate-list inclusion; both fields remain available to API/UI consumers.
- Discovery follows at most eight CNAME hops, never resolves a name twice, continues after a failed later lookup when an A record was already found, and raises only when no IP was found. Redirect behavior remains unchanged unless a focused test proves it is unnecessary.

## Testing

Tests are added before each production change and must fail against the baseline. Focused tests cover all confirmed defects, CNAME cycles/depth, partial discovery failure, and the existing FAILOVER pending-win rule. The full pytest suite runs before and after each commit. No parallel benchmark or transaction refactor is included unless separately proven and committed.

## Rollback

Each implementation commit is self-contained and can be reverted independently. The complete branch can be restored to the original commit `bb7fe5495fb8d9427c685d8f7eee445ba76a07e`.
