# DRO Optimization Notes

## Baseline

- Original commit SHA: `bb7fe5495fb8d9427c685d8f7eee445ba76a07e`
- Date: 2026-09-27
- Branch: `codex/optimize-core`
- Baseline command: `python -m pytest -q`
- Baseline result: `103 passed, 4 skipped, 1 warning` (Starlette/httpx deprecation warning)

## Planned files and risks

| File | Planned change | Risk | Rollback |
|---|---|---|---|
| `app/core/optimizer.py` | AdGuard endpoint guard and rewrite inclusion semantics | Low: return/logging and summary-field behavior | `git checkout bb7fe5495fb8d9427c685d8f7eee445ba76a07e -- app/core/optimizer.py` |
| `app/integrations/adguard.py` | Preserve zero-vs-multiple endpoint reason for callers | Low: exception message only | `git checkout bb7fe5495fb8d9427c685d8f7eee445ba76a07e -- app/integrations/adguard.py` |
| `app/core/benchmark.py` | Correct statistics for 0/1/2 samples | Low: aggregate jitter edge cases | `git checkout bb7fe5495fb8d9427c685d8f7eee445ba76a07e -- app/core/benchmark.py` |
| `app/core/decision.py` | Total ranking key and whitespace lock handling | Medium: candidate ordering/decision lock boundary | `git checkout bb7fe5495fb8d9427c685d8f7eee445ba76a07e -- app/core/decision.py` |
| `app/core/discovery.py` | CNAME cycle/depth and partial-failure handling | Medium: DNS candidate set and error behavior | `git checkout bb7fe5495fb8d9427c685d8f7eee445ba76a07e -- app/core/discovery.py` |
| `tests/test_core.py` | Regression coverage for core behavior | Low: test-only | `git checkout bb7fe5495fb8d9427c685d8f7eee445ba76a07e -- tests/test_core.py` |
| `tests/test_optimizer.py` | AdGuard and optimizer regression coverage | Low: test-only | `git checkout bb7fe5495fb8d9427c685d8f7eee445ba76a07e -- tests/test_optimizer.py` |
| `tests/test_benchmark.py` | New statistics-focused tests if needed | Low: test-only | `git checkout bb7fe5495fb8d9427c685d8f7eee445ba76a07e -- tests/test_benchmark.py` |
| `docs/optimization-notes.md` | Evidence, risks, rollback, and final results | None | Revert the documentation commit |

No dependency, secret, environment file, schema, migration, remote, or public API signature changes are planned.

## Open questions

- Whether `current_rewrite_included` is consumed externally as a public semantic field is not fully documented. The implementation will preserve its existing candidate-list meaning and use the already-present `current_rewrite_in_public_dns` field for the public-DNS meaning.
- Benchmark parallelism, client context managers, and transaction consolidation are intentionally deferred until caller mapping and tests prove they are safe.

## Commit and rollback policy

Each behavior cluster will be a small commit with a body describing why, old behavior, new behavior, and rollback. To undo the entire work, revert the implementation commits or restore the original SHA and listed paths.
