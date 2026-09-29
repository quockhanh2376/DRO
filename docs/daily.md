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

- Current package version: `v1.0.0-rc.1`; production deployment is running the reviewed working-tree build. Service status is active; application health returned `{"status":"ok"}`.
- Production host: `172.16.10.9`; app: `/opt/dro`; DB: `/var/lib/dro/dro.db`; env: `/etc/dro/dro.env`; service: `dro.service`; port: `18081`.
- Public UI: [https://dro.aswigsyd.int](https://dro.aswigsyd.int); AdGuard UI: [https://adguard10.aswigsyd.int](https://adguard10.aswigsyd.int). Nginx terminates HTTPS on port 443. DRO backend is bound to `127.0.0.1:18081`; its AdGuard API URL is internal at `http://127.0.0.1:3001`.
- DB migration is `0006_auto_apply`; a pre-deploy SQLite backup was made at `/var/lib/dro/dro.db.pre-deploy-20260927`. Existing DB data was preserved and migrated in place. `/etc/dro/dro.env` remains `root:root`, mode `600`; secret values were not displayed.
- `dro.service` listens only on `127.0.0.1:18081`; `/health` is checked through the HTTPS proxy. Uvicorn trusts forwarded headers from loopback only. `DRO_HTTPS_ENABLED=true` makes session cookies Secure, HttpOnly, and SameSite=Lax.
- `dns-optimizer.timer` is disabled/inactive. `/opt/dns-optimizer` remains intact for rollback.
- UFW is enabled. LAN rules allow DNS 53 TCP/UDP, AdGuard UI 80, and DRO 18081. SSH is allowed, but current UFW rules allow SSH from Anywhere; restrict it to the admin LAN when practical.
- At the earlier checkpoint below, scheduler state was disabled. Final RC validation found the production scheduler enabled, with a 60-minute default interval; target-specific intervals govern scheduled runs. Runtime Settings persist the scheduler, interval, and retention options.
- At this earlier checkpoint, samples used 30-day and runs 180-day retention. The final release candidate below uses configurable 72-hour benchmark-history retention for runs/results/samples together; rewrite history remains indefinite.

### Completed today

- Fixed installer Python selection for Python 3.12+ and added coverage for 3.11 rejection and 3.12/3.13/3.14 acceptance.
- Added web redirect-to-login behavior while keeping API authentication errors as JSON 401.
- Added target-list HTMX Run Now with an inline result, and editable persisted Settings for the default interval, scheduler state, and log retention.
- Deployed the current tree to `/opt/dro`; applied migration 0005 after preserving a DB backup. Authenticated Run Now returned the saved inline result fields (candidates, best IP, statistics, decision, reason, and timestamp). No DNS rewrite mutation was performed.
- AdGuard read-only check succeeded and returned two rewrites. Production pytest at that deployment: 62 passed, 1 deprecation warning. Local final pytest: 58 passed, 4 skipped, 1 deprecation warning.

### Known limitations and next tasks

- Decide internal names: `dro.aswigsyd.int` and `adguard.aswigsyd.int`.
- Add HTTPS reverse proxy and internal CA certificate, then enable Secure cookies and bind Uvicorn to localhost.
- Monitor the first scheduled benchmark cycles after enabling the scheduler. Keep `/opt/dns-optimizer` as rollback until DRO proves stable.
- SSH currently has a world-open UFW rule. Restrict it to the management LAN.
- Keep Nginx proxy headers aligned with the loopback-only Uvicorn trust configuration when maintaining either virtual host.

### HTTPS topology deployment verification

- Set production `ADGUARD_URL` to internal `http://127.0.0.1:3001` and `DRO_HTTPS_ENABLED=true`, preserving all other `/etc/dro/dro.env` values and its `root:root 600` permissions.
- Deployed the latest tested code. `dro.service` is active and bound only to `127.0.0.1:18081`; the conflicting `dro-phase5-validation.service` was disabled while its files under `/opt/dro/phase5-validation` were left intact.
- Nginx sends `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto`; Uvicorn trusts forwarded headers only from loopback. HTTPS login, Secure/HttpOnly/SameSite cookie flags, and CSRF-protected Run Now were verified through `https://dro.aswigsyd.int`.
- Current rewrite lookup now returns `app.practicemanager.xero.com -> 113.171.12.186` from AdGuard. Read-only Web Run Now showed the current rewrite inline and rendered Apply Best IP when its conditions were met. Apply was not clicked; no DNS rewrite changed.
- Production pytest: 71 passed, 1 deprecation warning. No migration was needed; database revision remained `0005_fractional_intervals`.

## 2026-09-27 — v1.0.0-rc.1 final review and validation

### Release state

- Release candidate `v1.0.0-rc.1` is built from the central version in `app/version.py` and is deployed on production. The web UI displays the release string; FastAPI metadata and package metadata use the corresponding normalized `1.0.0rc1` version.
- Supported product scope remains HTTPS/Web targets on one DRO host. No TCP/RDS, distributed agent, or container work is included.
- Production topology: `https://dro.aswigsyd.int` through Nginx to `127.0.0.1:18081`; `https://adguard10.aswigsyd.int` through Nginx to AdGuard UI/API backend `127.0.0.1:3001`. Nginx configuration was not changed.
- Host: `172.16.10.9`; app: `/opt/dro`; database: `/var/lib/dro/dro.db`; credentials: `/etc/dro/dro.env`; logs: `/var/log/dro`; service: `dro.service`; public HTTPS port: `443`; backend port: `18081`.
- Host and service timezone is `Asia/Ho_Chi_Minh` (UTC+7). SQLite timestamps remain UTC; UI timestamps display in ICT. Historical database timestamps were not rewritten.
- Current persisted runtime settings: scheduler enabled; default interval 60 minutes; log retention 5 days; benchmark history retention 72 hours; maximum 5 automatic rewrites/day. The global Automatic DNS Rewrite master and Auto Apply for both current targets are enabled. Existing decision, health, readback, rate-limit, lock, and rollback safeguards remain active.
- Application log retention is 7 days. Benchmark runs, aggregate results, and samples are cleaned together after the configured retention (default 72 hours); rewrite history remains indefinitely.

### Final validation

- Local full pytest: 103 passed, 4 skipped. Focused security/UI/optimizer/queue/ping tests: 83 passed. One upstream Starlette/httpx deprecation warning remains.
- Ubuntu full pytest under the `dro` account: 107 passed, one upstream deprecation warning. Python compile/import checks passed. Temporary SQLite migration upgrade, downgrade, and re-upgrade passed; production was already at `0006_auto_apply`, so no schema migration was needed.
- Production service is enabled and active, HTTPS `/health` returns `{"status":"ok"}`, and the UI opens after login. The backend listens only on `127.0.0.1:18081`; the host reports `Asia/Ho_Chi_Minh`; the UI displays the version and ICT timestamps.
- Read-only AdGuard lookups succeeded for both configured targets, and displayed Current IP matched AdGuard for 2/2 targets. A production Run Now benchmark completed and persisted its result. Production Ping start/stop and Apply button eligibility were verified. No Apply action or DNS rewrite was performed.
- Logrotate dry-run is valid. Ubuntu pytest covers installer behavior and SQLite backup/restore using temporary databases. Production `/etc/dro/dro.env` remains `root:root 600`; `/var/lib/dro/dro.db` remains `dro:dro 600`; `/var/log/dro` remains `dro:dro 750`.
- Legacy `dns-optimizer.timer` remains inactive and `/opt/dns-optimizer` was not modified. Production database integrity check returned `ok`.

### Known limitations and rollback

- Benchmark queue and live ping process tracking are in-memory and assume the single configured DRO service instance. The scheduler remains disabled pending deliberate production monitoring.
- SSH is still allowed from any source by the existing firewall configuration; restrict it to the management LAN when practical.
- No production DNS change was made for release validation. If the candidate must be rolled back, restore the previous application files under `/opt/dro` and restart `dro.service`; keep the current database because this release did not change its schema. `/opt/dns-optimizer` remains available as a separate legacy rollback path.

### Next recommended checks

- Commit and publish the validated release candidate with tag `v1.0.0-rc.1`.
- Review SSH firewall scope and monitor scheduled benchmark windows and automatic rewrite audit events.
- Monitor the first scheduled cycles and verify any future rewrite manually from the UI; retain the legacy optimizer until DRO is proven stable.

## 2026-09-29 — Performance audit

- Audited benchmark, DNS discovery, scheduler/queue, logging, and persistence paths; added the reproducible read-only `scripts/performance_audit.py` harness plus a deterministic instrumentation test.
- Three network repeats: median batch wall was 9.12 s (2 candidates), 18.14 s (4 candidates), and 11.13 s (two targets concurrently, 2 candidates each). All 300 HTTPS samples were valid.
- Measured top costs: HTTPS/curl sample work (5.77–14.46 s per target); local unavailable-AdGuard lookup (2.50–2.74 s, environment-specific); curl process/wrapper delta estimate 57–75 ms/sample. DoH was 115–158 ms; log handlers, DB transaction, and `summarize()` were negligible.
- No runtime optimization was justified: concurrency risks benchmark distortion; curl batching/replacement risks TLS/retry/timeout/error semantics; other measured costs were small. No DNS was changed.
- Validation: full pytest passed (result recorded in the audit report). Production was not accessed or changed.

## 2026-09-29 - Native benchmark client experiment

- Created `experiment/native-benchmark-client` from `origin/main` at `8f702dc066e953a1afeb5282643cf6fcc2643c1f`; the production/default runner remains curl. Added an opt-in standard-library native HTTPS prototype with direct candidate-IP socket pinning, original-host SNI/verification, HTTP/1.1, a total deadline, accepted 200-399 status policy, no redirect following, and one connection per sample.
- Added local TLS regression tests for CA and hostname verification, SNI/Host, fresh connections, redirects/status, read and total timeouts, refused/reset error classification, and an assertion that curl stays the default with its timing fields. Focused native/audit suite: 9 passed.
- Five paired network repeats per case used the same discovered IPs for both clients. Every one of 500 samples per client was valid. Batch medians (curl/native): A 5.859/7.118 s; B 14.938/15.220 s; C 7.431/7.595 s. Native was slower in all cases; its best-IP ranking agreed with curl in 6 of 20 matched target-run comparisons.
- Per-case total CPU (curl parent + curl children / native process) was A 2.750/2.859 s, B 5.921/5.344 s, C 5.531/5.312 s. Shared Python high-water RSS was about 43-50 MiB; individual curl children peaked at about 8.3 MiB. There was no consistent performance win.
- Recommendation: keep curl. Windows Schannel revocation fallback and platform trust-backend differences remain relevant; subprocess elimination did not justify slower/unstable benchmark results. See `docs/performance-native-client-experiment.md`.
- Full pytest: 167 passed, 4 skipped, 1 upstream Starlette/httpx deprecation warning. No production deploy, DNS rewrite, DB change, or merge was performed.
