# DRO - DNS Route Optimizer
# DRO — DNS Route Optimizer

## Development setup

Requires Python 3.12+ and curl on PATH.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## Run tests

```powershell
python -m pytest
```

## Ubuntu live validation (read-only)

From the repository on Ubuntu:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
sudo install -d -o root -g root -m 700 /etc/dro
sudo install -o root -g root -m 600 /dev/null /etc/dro/dro.env
sudoedit /etc/dro/dro.env
sudo chown root:root /etc/dro/dro.env
sudo chmod 600 /etc/dro/dro.env
dro adguard check
```

If no configured or localhost instance is detected, subnet discovery is manual and on-demand:

```sh
dro adguard discover --subnet 192.168.1.0/24
```

If several choices are returned, set the selected endpoint in `/etc/dro/dro.env` and rerun the check. Then benchmark any hostname:

```sh
dro benchmark go.fyi.app
dro benchmark app.practicemanager.xero.com
dro benchmark example.com
```

`dro adguard check` performs only a rewrite-list read. `dro benchmark` reads the current rewrite when AdGuard is available, adds its IP to public DNS candidates, benchmarks the candidates, and prints a decision. Both commands are read-only and never apply a DNS change.

## AdGuard credentials

Store real credentials only in `/etc/dro/dro.env`. The service reads this file at runtime; process environment values override file entries.

```sh
sudo install -d -o root -g root -m 700 /etc/dro
sudo install -o root -g root -m 600 /dev/null /etc/dro/dro.env
sudoedit /etc/dro/dro.env
sudo chown root:root /etc/dro/dro.env
sudo chmod 600 /etc/dro/dro.env
```

Use `.env.example` for placeholder names only. Never put stored passwords in logs, API responses, or UI output.

## Linux service logging

The CLI writes concise application logs to standard error, so systemd captures them in the journal without a file logger:

```ini
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dro
```

View logs with `journalctl -u dro.service`. To keep a seven-day file log instead, configure the service to write under `/var/log/dro/` and install `scripts/dro-logrotate` as `/etc/logrotate.d/dro`. The rotation policy retains seven daily logs. Never put AdGuard credentials in command-line arguments or log messages.

Set `DRO_DEBUG=1` to enable per-run diagnostic details. Normal logging reports discovery and benchmark summaries, unhealthy candidates, decisions, and AdGuard API errors.
