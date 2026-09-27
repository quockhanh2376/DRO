# DRO — DNS Route Optimizer

DRO supports HTTPS/Web targets only. It discovers and benchmarks public HTTPS endpoints, stores results in SQLite, and can safely update explicitly configured AdGuard Home rewrites. The service is intended for one Ubuntu host and one local administrator.

## Development setup

Requires Python 3.12+ and `curl` on `PATH`.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
alembic upgrade head
python -m pytest
```

Set `DRO_DB_PATH=data/dev.db` for a local database. On Linux the default database is `/var/lib/dro/dro.db`.

## Credentials and login

Real credentials belong only in `/etc/dro/dro.env`. Keep the file owned by `root:root` with mode `600`. It may contain AdGuard credentials, `DRO_ADMIN_USER`, `DRO_ADMIN_PASSWORD`, `DRO_SESSION_SECRET`, and `DRO_HTTPS_ENABLED`.

```sh
sudo install -d -o root -g root -m 700 /etc/dro
sudo install -o root -g root -m 600 /dev/null /etc/dro/dro.env  # only if it does not already exist
sudoedit /etc/dro/dro.env
```

Set a unique long admin password and generate the session signing key with `openssl rand -hex 32`. `DRO_ADMIN_PASSWORD` initializes the local admin credential; SQLite stores a scrypt hash, never the plaintext password. The admin username defaults to `admin`. `DRO_SESSION_SECRET` signs the HttpOnly, SameSite=Lax session cookie. Set `DRO_HTTPS_ENABLED=true` when HTTPS terminates in front of DRO so the cookie gets the Secure flag. Rotating the session signing secret ends active sessions.

Example value generator (run locally on the Ubuntu host and place its output in the root-only env file):

```sh
openssl rand -hex 32
```

Protect the file and migrate the database:

```sh
sudo chown root:root /etc/dro/dro.env
sudo chmod 600 /etc/dro/dro.env
alembic upgrade head
```

The web pages and API require login except `/login`, `/health`, and `/api/v1/system/status`. All mutation routes require a valid CSRF token. Login initializes the admin hash the first time the configured credentials are used.

## Current Production State (2026-09-27)

DRO release candidate `v1.0.0-rc.1` is deployed on `172.16.10.9` at `/opt/dro`, with database `/var/lib/dro/dro.db`, secrets in `/etc/dro/dro.env` (`root:root`, mode `600`), service `dro.service`, and backend `127.0.0.1:18081`. The authenticated UI is at [https://dro.aswigsyd.int](https://dro.aswigsyd.int); AdGuard Home is at [https://adguard10.aswigsyd.int](https://adguard10.aswigsyd.int), and DRO accesses the AdGuard API internally at `http://127.0.0.1:3001`. Nginx terminates TLS on port 443 and proxies by hostname. Uvicorn trusts forwarded proxy headers only from loopback, remains bound to loopback, and production sets `DRO_HTTPS_ENABLED=true` for Secure session cookies. Host and service timezone is `Asia/Ho_Chi_Minh`; UI timestamps are displayed in ICT while SQLite timestamps remain UTC. The deployed database schema is managed by Alembic.

Production currently has the scheduler enabled, a 60-minute default interval, 5-day application log retention, benchmark-history retention of 72 hours, and a five-per-day automatic rewrite limit. Per-target intervals govern scheduled runs. The global Automatic DNS Rewrite gate and per-target Auto Apply are enabled for the two configured targets; existing safety rules still apply. Benchmark runs, aggregate results, and samples are retained together for the configured history period; Runtime Settings allows an hours/days override. Rewrite history is retained indefinitely. The deployment includes the HTMX inline Run Now result, verified Apply Best IP action, and editable interval, scheduler, and retention settings. Manual Run Now benchmarks remain read-only. The legacy `dns-optimizer.timer` is disabled, while `/opt/dns-optimizer` remains available for rollback.

Nginx must forward `Host $host`, `X-Forwarded-Host $host`, and `X-Forwarded-Proto $scheme`. Uvicorn accepts proxy headers only from `127.0.0.1`; keep the backend listener loopback-only. Set `DRO_HTTPS_ENABLED=true` in `/etc/dro/dro.env` so session cookies are Secure.

## Ubuntu service installation

Configure `/etc/dro/dro.env`, then run from a repository checkout:

```sh
sudo bash scripts/install-ubuntu.sh
sudo systemctl status dro.service
sudo journalctl -u dro.service
```

The installer uses a dedicated `dro` account, places the app in `/opt/dro`, data in `/var/lib/dro`, and logs in `/var/log/dro`. It installs `systemd/dro.service`, enables restart-on-failure, and does not access or alter `/opt/dns-optimizer`. Re-running it preserves `/etc/dro/dro.env`. It requires Python 3.12+ and the admin password and session signing secret before starting the service. For a separate validation instance, override `DRO_INSTALL_DIR`, `DRO_SERVICE_NAME`, `DRO_DATABASE_PATH`, `DRO_LOG_FILE`, `DRO_LOGROTATE_NAME`, `DRO_LISTEN_PORT`, and optionally `DRO_PYTHON`; paths are restricted to the corresponding `/opt/dro`, `/var/lib/dro`, and `/var/log/dro` trees.

The installer template binds to `127.0.0.1:8000`; use an HTTPS reverse proxy for remote access and set `DRO_HTTPS_ENABLED=true`. It runs Alembic migrations before starting. Uvicorn access logging is disabled. Diagnostics go to `/var/log/dro/dro.log`; logrotate rotates daily and the application prunes rotated logs according to the saved retention setting (7 days by default).

## Scheduler, locks, and DNS changes

The scheduler is disabled until an admin enables it in **Runtime Settings** on Dashboard. Each enabled target uses its `interval_hours`; Run Now uses the same per-target concurrency guard. Scheduler last/next run times are persisted. The guard is process-local; run one DRO service instance.

Targets in `monitor` and `recommend` mode never write DNS. In `auto` mode, qualified `UPDATE` or `FAILOVER` decisions may write a rewrite unless an IP is manually locked or the default cap of four automatic rewrites in the previous 24 hours is reached. The daily cap is configurable in Settings. Every automatic change gets an immediate three-run health check; DRO restores the previous rewrite if it fails. Manual rollback also checks the restored address. Locking can use the live current rewrite or a specified IPv4 address; locked targets continue to benchmark.

## Backup, restore, and retention

SQLite backup uses SQLite’s online backup API and contains only the database, never `/etc/dro/dro.env`:

```sh
sudo -u dro /opt/dro/.venv/bin/dro db backup /var/lib/dro/backups/dro-$(date +%F).db
```

Stop the service before restore, validate the backup, confirm replacement, then restart:

```sh
sudo systemctl stop dro.service
sudo -u dro /opt/dro/.venv/bin/dro db restore /var/lib/dro/backups/dro-YYYY-MM-DD.db
# Type RESTORE when prompted, or pass --yes after confirming the path.
sudo systemctl start dro.service
```

Restore stages and validates the SQLite file before replacing the live database. Keep backups outside the repository and restrict their permissions. Benchmark runs, results, and samples expire together after the configurable history retention (72 hours by default); rewrite history is retained indefinitely. `dro cleanup` runs the same cleanup immediately.

## CLI

```sh
dro adguard check
dro benchmark go.fyi.app
dro db backup /secure/backups/dro.db
dro db restore /secure/backups/dro.db
dro cleanup
```

`dro benchmark` remains read-only. Automatic rewrites are performed by the authenticated API and scheduler when a target is in Auto mode.
