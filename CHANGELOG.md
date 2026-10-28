# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-10-28

### Performance Improvements
- **Optimized Dashboard Queries**: Replaced $O(N)$ sequential queries on dashboard load with a single window-function query (`row_number() over (partition by target_id ...)`) and `selectinload(results)`. Batch-fetched optimizer and schedule states, bounding total dashboard queries to $\le 5$ regardless of target count.
- **Non-blocking Authentication**: Offloaded CPU-heavy scrypt password hashing and verification to threadpool workers (`run_in_threadpool`) to keep the ASGI event loop responsive during authentication. Added process-level credential verification cache.
- **Constant-time Unknown User Handling**: Replaced dynamic dummy hash generation with a static pre-computed scrypt constant-time dummy verify to eliminate timing attack vectors and CPU spikes on failed logins.
- **Run & Rewrite History Pagination**: Added `limit` (default 50, max 500) and `offset` support to database repositories, REST API endpoints (`/api/v1/runs`, `/api/v1/rewrite-history`), and web routes.
- **Secret Sanitization Caching**: Cached secret value collections with snapshot tracking across log/storage sanitization passes. Added guardrails against redacting valid IP addresses and short tokens (< 4 characters).

### Security Hardening
- **Login Rate Limiting**: Added in-memory `LoginRateLimiter` allowing up to 5 failed attempts per minute per client IP with a 15-minute temporary lockout.
- **Audit Logging for Auth Failures**: Recorded `login_failed` audit events with client IP and reason on invalid credentials or rate-limited requests.
- **Internal System Metrics Protection**: Hardened `/api/v1/system/status` with `require_admin` dependency to prevent unauthorized exposure of internal system metrics (target counts, benchmark counts, and scheduler state). Kept `/health` public for reverse proxy health checks.
- **HTTPS Security Headers**: Added standard security headers middleware:
  - `X-Frame-Options: DENY`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: same-origin`
  - `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`
- **Session Secret Warning & Installer Provisioning**: Log warning on application startup if `DRO_SESSION_SECRET` is unset. Updated `scripts/install-ubuntu.sh` to generate a secure random 48-byte URL-safe secret in `/etc/dro/dro.env` if missing.
- **Logging Pipeline Preservation**: Configured `disable_existing_loggers=False` in `migrations/env.py` so Alembic runs do not disable application loggers.

## [1.0.8] - 2026-10-13
- Read-only System Diagnostics UI and endpoint (`/diagnostics/run`).
- Regional DNS checker integration (`/dns-checker`).
- AdGuard rewrite synchronization and live target ranking.
- Safe DNS rewrite creation flow (Add to DNS).
- Localized Vietnam Time (UTC+7) timestamp display.

## [1.0.0-rc.1] - 2026-09-27
- Initial release candidate for production deployment.
