# Development log

## 2026-09-26 — v0.1.0 Core Optimizer

- Added validated Pydantic target and benchmark/decision models.
- Implemented public Google DNS-over-HTTPS discovery, curl-based HTTPS benchmarking, statistics, and decision rules.
- Added AdGuard DNS rewrite API client and a read-only `dro benchmark` CLI.
- Added unit tests and documented local setup and usage.
- At v0.1.0, persistence, API, UI, scheduler, and deployment had not yet been implemented.

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

## 2026-09-27 — Production deployment and handoff

### Current state

- Current package version: `0.1.0`; production deployment is running the reviewed working-tree build. Service status is active; application health returned `{"status":"ok"}`.
- Production host: `172.16.10.9`; app: `/opt/dro`; DB: `/var/lib/dro/dro.db`; env: `/etc/dro/dro.env`; service: `dro.service`; port: `18081`.
- Access URL: [http://172.16.10.9:18081](http://172.16.10.9:18081). AdGuard UI: [http://172.16.10.9](http://172.16.10.9). DRO AdGuard API URL: `http://127.0.0.1`.
- DB migration is `0005_fractional_intervals`; a pre-deploy SQLite backup was made at `/var/lib/dro/dro.db.pre-deploy-20260927`. Existing DB data was preserved and migrated in place. `/etc/dro/dro.env` remains `root:root`, mode `600`; secret values were not displayed.
- `dro.service` listens on `0.0.0.0:18081` and `/health` is healthy. Production currently uses direct HTTP; HTTPS proxying and localhost-only Uvicorn binding are pending.
- `dns-optimizer.timer` is disabled/inactive. `/opt/dns-optimizer` remains intact for rollback.
- UFW is enabled. LAN rules allow DNS 53 TCP/UDP, AdGuard UI 80, and DRO 18081. SSH is allowed, but current UFW rules allow SSH from Anywhere; restrict it to the admin LAN when practical.
- Scheduler is disabled (persisted setting `false`); Run Now remains available. The default interval is two hours when no saved override exists; target-specific intervals govern scheduled runs. Settings now allow the default interval in minutes/hours, scheduler toggle, and log retention edits.
- Application log retention defaults to 7 days; benchmark samples 30 days; benchmark runs 180 days; rewrite history is retained indefinitely.

### Completed today

- Fixed installer Python selection for Python 3.12+ and added coverage for 3.11 rejection and 3.12/3.13/3.14 acceptance.
- Added web redirect-to-login behavior while keeping API authentication errors as JSON 401.
- Added target-list HTMX Run Now with an inline result, and editable persisted Settings for the default interval, scheduler state, and log retention.
- Deployed the current tree to `/opt/dro`; applied migration 0005 after preserving a DB backup. Authenticated Run Now returned the saved inline result fields (candidates, best IP, statistics, decision, reason, and timestamp). No DNS rewrite mutation was performed.
- AdGuard read-only check succeeded at `http://127.0.0.1` and returned two rewrites. Production pytest: 62 passed, 1 deprecation warning. Local final pytest: 58 passed, 4 skipped, 1 deprecation warning.

### Known limitations and next tasks

- Decide internal names: `dro.aswigsyd.int` and `adguard.aswigsyd.int`.
- Add HTTPS reverse proxy and internal CA certificate, then enable Secure cookies and bind Uvicorn to localhost.
- Monitor the first scheduled benchmark cycles after enabling the scheduler. Keep `/opt/dns-optimizer` as rollback until DRO proves stable.
- SSH currently has a world-open UFW rule. Restrict it to the management LAN.
- The installer-generated default service unit binds to localhost, but production currently uses the required direct `0.0.0.0:18081` listener; reconcile the unit configuration when introducing the reverse proxy.
