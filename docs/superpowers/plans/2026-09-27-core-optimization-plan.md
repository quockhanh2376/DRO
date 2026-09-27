# DRO Core Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the confirmed DRO core defects with regression tests and preserve existing API, schema, and decision policy.

**Architecture:** Keep the current FastAPI/SQLAlchemy/core-module boundaries. Changes remain local to benchmark, decision, discovery, optimizer, and tests; optional optimizations are excluded unless independently proven.

**Tech Stack:** Python 3.12+, pytest, httpx, SQLAlchemy, Pydantic.

## Global Constraints

- Work only on branch `codex/optimize-core` from `bb7fe5495fb8d9427c685d8f7eee445ba76a07e`.
- Do not change public API contracts or database schema.
- Do not change healthy thresholds, decision policy, consecutive wins, manual lock semantics for valid IPs, failover policy, or curl TLS flags.
- Do not add dependencies or touch secrets.
- Every behavior change gets a failing regression test before production code.
- Run `python -m pytest -q` before handoff and record exact results in `docs/optimization-notes.md`.

---

### Task 1: AdGuard lookup guards and rewrite reporting

**Files:**
- Modify: `app/core/optimizer.py`
- Test: `tests/test_optimizer.py`

**Interfaces:**
- Preserve `read_adguard_rewrite(hostname: str) -> tuple[str | None, bool>`.
- Preserve `run_benchmark_cycle` response fields and distinguish public-DNS membership from candidate inclusion.

- [ ] **Step 1: Add failing tests** for zero and multiple discovered AdGuard endpoints, asserting `(None, False)`, distinct warnings, and no client mutation; add a cycle test asserting public-DNS membership is computed before current-IP candidate append.
- [ ] **Step 2: Run focused tests** with `python -m pytest tests/test_optimizer.py -q` and record the expected baseline failures.
- [ ] **Step 3: Implement the smallest guards** around `discover_adguard` and preserve the existing client close/error behavior.
- [ ] **Step 4: Run focused and related tests** and verify all pass.
- [ ] **Step 5: Commit** with message `fix(core): guard ambiguous AdGuard discovery` and a body covering old/new behavior and rollback.

### Task 2: Statistics, ranking, and manual lock boundaries

**Files:**
- Modify: `app/core/benchmark.py`, `app/core/decision.py`
- Test: `tests/test_core.py`, optionally create `tests/test_benchmark.py` if the existing layout has no focused benchmark module

**Interfaces:**
- Preserve `calculate_statistics` and `rank_candidates` signatures.
- Preserve valid-IP manual lock behavior and existing FAILOVER pending-win behavior.

- [ ] **Step 1: Add failing tests** for zero/one/two samples, healthy candidates with `median_ms` or `jitter_ms` set to `None`, lock values `None`, `""`, `"  "`, and a real IPv4, plus FAILOVER pending-state behavior.
- [ ] **Step 2: Run focused tests** and verify each new regression test fails for the baseline defect.
- [ ] **Step 3: Implement minimal edge-case handling** without altering the health rule or decision thresholds.
- [ ] **Step 4: Run focused tests and the full suite**; inspect ordering and action assertions.
- [ ] **Step 5: Commit** with message `fix(core): harden statistics ranking and locks` and rollback details in the body.

### Task 3: CNAME cycle, depth, and partial-failure handling

**Files:**
- Modify: `app/core/discovery.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Preserve `PublicDnsDiscovery.discover(hostname: str) -> list[str]` and `DiscoveryError`.

- [ ] **Step 1: Add failing tests** for a CNAME cycle, more than eight CNAME hops, a failed later lookup after an earlier A record, and a lookup failure with no IP.
- [ ] **Step 2: Run the focused discovery tests** and verify they fail by hanging/raising/returning the wrong result as applicable, with bounded test doubles.
- [ ] **Step 3: Implement the eight-hop bound, existing-name suppression, and conditional continuation/raise behavior.
- [ ] **Step 4: Run discovery tests and the full suite**, confirming no existing malformed-response behavior regresses.
- [ ] **Step 5: Commit** with message `fix(core): bound public DNS CNAME discovery` and rollback details in the body.

### Task 4: Documentation and final verification

**Files:**
- Modify: `docs/optimization-notes.md`

- [ ] **Step 1: Run `python -m pytest -q` and any configured lint/typecheck commands available in `pyproject.toml`.**
- [ ] **Step 2: Record final SHA, commit list, test output, intentional non-changes, and open questions in the notes.**
- [ ] **Step 3: Inspect `git diff`, `git status`, and commit history for secrets, unrelated files, and rollback-complete commits.**
