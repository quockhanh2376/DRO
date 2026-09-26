# Development log

## 2026-09-26 — v0.1.0 Core Optimizer

- Added validated Pydantic target and benchmark/decision models.
- Implemented public Google DNS-over-HTTPS discovery, curl-based HTTPS benchmarking, statistics, and decision rules.
- Added AdGuard DNS rewrite API client and a read-only `dro benchmark` CLI.
- Added unit tests and documented local setup and usage.
- At v0.1.0, persistence, API, UI, scheduler, TCP/RDS, and deployment had not yet been implemented.

## 2026-09-26 — Phase 2 SQLite persistence

- Added SQLAlchemy 2 models for targets, benchmark runs/results/samples, optimizer state, rewrite history, settings, and audit events.
- Added transaction-scoped SQLite sessions, repository functions, and an initial reversible Alembic migration.
- Connected the read-only benchmark CLI to stored target settings, candidate results, decision reasons, current rewrite state, and consecutive-win state.
- Added temporary SQLite and migration round-trip tests. No DNS mutation, cleanup jobs, API, or UI were added.
- Database files are created with owner-only permissions; the schema contains no credential fields.

## 2026-09-26 — Phase 3 FastAPI API

- Added an ASGI app entry point and health/system status endpoints.
- Added target CRUD, read-only manual benchmark, benchmark/run history, rewrite history, and current rewrite lookup.
- Extracted the shared read-only benchmark cycle for CLI/API reuse; routes contain no decision logic and no DNS mutation.
- Added TestClient coverage with temporary SQLite. Web UI and authentication remain out of scope.

## 2026-09-26 — Phase 4 Web UI

- Added the Jinja2/HTMX dashboard, target management, target detail, history, and safe settings pages.
- Added responsive dark styling and UI tests for rendering, target forms, validation, and secret redaction.
- Reused existing target, benchmark, and history services. DNS apply and lock controls were deferred until Phase 5.

## 2026-09-26 — Phase 5 Production hardening

- Added a single local admin credential using a persisted scrypt hash, signed HttpOnly/SameSite sessions, optional Secure cookies, and CSRF checks on every mutation.
- Added audited manual IP lock/unlock, confirmed rollback, immediate post-change health checks with automatic restore, and a default cap of four automatic rewrites per day.
- Added a persisted, toggleable single-process scheduler with per-target intervals and duplicate-run prevention; added 30-day sample and 180-day benchmark-run cleanup while retaining rewrite history.
- Added validated SQLite backup/restore CLI commands and safe Ubuntu systemd/installer files using a dedicated `dro` account.
- Updated production setup, secrets, backup/restore, retention, and scheduler documentation. No DNS was changed during implementation or tests.
