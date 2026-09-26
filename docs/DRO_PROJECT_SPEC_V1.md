# DRO — DNS Route Optimizer
## Project Specification v1.0

### 1. Goal
DRO is an internal DNS endpoint optimization service that runs alongside AdGuard Home. It discovers candidate IPs for configured hostnames, benchmarks them from the DRO server location, decides whether a candidate is materially better than the current DNS rewrite, and safely updates AdGuard Home when policy allows.

Initial targets:
- `go.fyi.app`
- `app.practicemanager.xero.com`

DRO does **not** replace AdGuard Home or CDN routing. It only optimizes explicitly configured targets.

### 2. Tech stack
- Python 3.12+
- FastAPI
- SQLite
- SQLAlchemy 2.x
- Alembic
- Pydantic
- httpx
- APScheduler
- Jinja2 + HTMX
- systemd
- AdGuard Home API

### 3. Architecture
```text
Web UI
  |
FastAPI
  |
  +-- Target service
  +-- Benchmark engine
  +-- Decision engine
  +-- Scheduler
  +-- AdGuard integration
  |
SQLite
  |
AdGuard Home DNS Rewrite API
```

### 4. Repository structure
```text
DRO/
├── app/
│   ├── main.py
│   ├── api/
│   ├── core/
│   │   ├── discovery.py
│   │   ├── benchmark.py
│   │   ├── decision.py
│   │   ├── scheduler.py
│   │   └── health.py
│   ├── integrations/
│   │   └── adguard.py
│   ├── db/
│   │   ├── database.py
│   │   ├── models.py
│   │   └── repositories.py
│   ├── models/
│   ├── services/
│   └── web/
│       ├── templates/
│       └── static/
├── tests/
│   ├── unit/
│   └── integration/
├── migrations/
├── scripts/
├── systemd/
├── data/
├── docs/
│   └── PROJECT_SPEC_V1.md
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

### 5. Target model
Each target stores:
- hostname
- enabled
- protocol
- port
- path
- mode: `monitor`, `recommend`, `auto`
- interval_hours
- runs_per_ip
- timeout_seconds
- switch_threshold_ms
- switch_threshold_percent
- required_consecutive_wins
- immediate_switch_if_current_unhealthy
- optional manual_lock_ip

Initial defaults:
```text
interval_hours = 2
runs_per_ip = 10
switch_threshold_ms = 50
switch_threshold_percent = 5
required_consecutive_wins = 2
immediate_switch_if_current_unhealthy = true
```

### 6. SQLite responsibilities
SQLite stores:
- targets
- benchmark runs
- benchmark aggregate results
- optional individual benchmark samples
- consecutive-win state
- current rewrite state
- rewrite history
- settings
- audit records

Suggested tables:
```text
targets
benchmark_runs
benchmark_results
benchmark_samples
optimizer_state
rewrite_history
settings
audit_log
```

### 7. Candidate discovery
For HTTPS targets:
1. Query public DNS-over-HTTPS to bypass local AdGuard rewrites.
2. Follow CNAMEs.
3. Collect A records.
4. Deduplicate IPs.
5. Add the current AdGuard rewrite even if public DNS no longer returns it.
6. Add manually configured IPs if present.

Initial resolver:
```text
https://dns.google/resolve?name=<host>&type=A
```

### 8. HTTPS benchmark
Each IP is tested with hostname/SNI preserved:

```bash
curl --http1.1   --no-keepalive   -H "Connection: close"   --resolve "hostname:443:IP"   https://hostname/
```

Metrics:
- TCP connect time
- TLS handshake time
- total time
- HTTP status

Default health rule:
- 10 runs per IP
- at least 80% successful
- accepted HTTP status: 200–399

Calculated statistics:
- Average
- Median
- Min
- Max
- Jitter

Candidate ordering:
1. lowest average
2. lowest median
3. lowest jitter

### 9. Decision engine
#### Current IP already best
Decision: `KEEP`

#### New IP is only slightly better
A new candidate must beat current by:
```text
>= 50 ms OR >= 5%
```
Otherwise: `KEEP`

#### New IP materially better
Requires:
```text
2 consecutive wins
```

Example:
```text
08:00 Candidate A wins -> 1/2 -> HOLD
10:00 Candidate A wins -> 2/2 -> UPDATE
```

If another candidate wins at 10:00:
```text
Candidate B -> 1/2
Candidate A streak resets
```

#### Current IP unhealthy
If a healthy alternative exists:
```text
FAILOVER immediately
```

#### Manual lock
When an IP is manually locked:
- keep benchmarking
- never auto-rewrite
- show health warning if locked IP fails

### 10. Rollback
After every automatic rewrite:
1. save old IP
2. save new IP
3. save benchmark reason
4. run post-change health check
5. if new IP fails, restore old IP
6. record rollback event

### 11. Change-rate protection
Add:
```text
max_auto_changes_per_day = 4
```

When exceeded, Auto mode temporarily behaves like Recommend mode.

### 12. AdGuard integration
DRO reads and modifies DNS rewrites via AdGuard Home API.

Credential file:
```text
/etc/dro/dro.env
```

Example:
```text
ADGUARD_URL=http://127.0.0.1
ADGUARD_USER=adman
ADGUARD_PASS=...
```

Rules:
- never commit credentials
- never log passwords
- do not expose password through API/UI

### 13. API v1
Base:
```text
/api/v1
```

Targets:
```text
GET    /targets
POST   /targets
GET    /targets/{id}
PATCH  /targets/{id}
DELETE /targets/{id}
```

Benchmark:
```text
POST /targets/{id}/run
GET  /targets/{id}/runs
GET  /runs/{run_id}
```

Rewrite:
```text
GET  /targets/{id}/rewrite
POST /targets/{id}/rewrite/apply
POST /targets/{id}/rewrite/rollback
POST /targets/{id}/rewrite/lock
POST /targets/{id}/rewrite/unlock
```

System:
```text
GET /health
GET /api/v1/system/status
```

### 14. Web UI v1
Pages:
- Dashboard
- Targets
- Target detail
- Benchmark history
- Rewrite history
- Settings

Dashboard should show:
```text
Current IP
Best candidate
Current average
Best average
Improvement
Win state (0/2, 1/2, 2/2)
Mode
Status
Last test
Next test
```

Modes:
- Monitor
- Recommend
- Auto

### 15. Logging
Application log retention:
```text
7 days
```

Audit history should remain in SQLite longer than application logs.

Audit events:
- target created/edited/deleted
- manual benchmark
- rewrite applied
- failover
- rollback
- lock/unlock
- settings changed

### 16. Security
- AdGuard secrets outside Git
- SQLite permissions limited to DRO service account
- no `curl -k` in production
- validate hostnames and IPs
- use subprocess argument arrays, never shell interpolation
- CSRF protection for forms
- authenticated mutation endpoints
- secure session cookies when HTTPS enabled
- rate-limit manual benchmark requests

### 17. Failure behavior
Public DNS unavailable:
- keep current rewrite
- record failed run

AdGuard API unavailable:
- benchmark normally
- save recommendation
- do not mutate DNS

All candidates unhealthy:
- keep existing rewrite
- mark target critical

Database unavailable:
- do not modify DNS

### 18. Initial target configuration
`go.fyi.app`
```text
protocol = https
port = 443
path = /
mode = auto
interval_hours = 2
runs_per_ip = 10
threshold = 50 ms OR 5%
required_consecutive_wins = 2
immediate_failover = true
```

`app.practicemanager.xero.com`
```text
protocol = https
port = 443
path = /
mode = auto
interval_hours = 2
runs_per_ip = 10
threshold = 50 ms OR 5%
required_consecutive_wins = 2
immediate_failover = true
```

### 19. Development phases
#### Phase 1 — Core engine
- Public DNS discovery
- HTTPS benchmark runner
- Statistics
- Decision engine
- AdGuard client
- Pydantic models
- Unit tests

#### Phase 2 — SQLite
- SQLAlchemy models
- Alembic
- repositories
- persisted state/history
- remove dependency on state.json

#### Phase 3 — FastAPI
- target CRUD
- manual benchmark endpoint
- health/status
- rewrite history

#### Phase 4 — Web UI
- dashboard
- targets
- history
- settings
- manual controls

#### Phase 5 — Production hardening
- authentication
- rollback
- change limits
- manual lock
- retention
- installer/systemd

#### Phase 6 — TCP/RDS targets
- TCP connect benchmark
- custom ports
- static candidate IP lists

### 20. v1 definition of done
DRO v1 is complete when:
1. Admin can add/edit targets from UI.
2. DRO discovers public candidate IPs.
3. DRO benchmarks automatically.
4. Results persist in SQLite.
5. Dashboard shows current vs best IP.
6. Threshold + consecutive-win logic works.
7. Auto mode updates AdGuard.
8. Current-IP failure triggers safe failover.
9. Every rewrite is audited.
10. Manual lock and rollback work.
11. DRO survives Ubuntu reboot.
12. Log retention works.
13. No secrets are committed.
14. Core decision logic has automated tests.

### 21. First milestone
Milestone:
```text
v0.1.0 — Core Optimizer
```

Tasks:
1. Create Pydantic Target model.
2. Implement `PublicDnsDiscovery`.
3. Implement `HttpsBenchmarkRunner`.
4. Implement statistics.
5. Implement `DecisionEngine`.
6. Implement `AdGuardClient`.
7. Add CLI command:
   ```bash
   dro benchmark go.fyi.app
   ```
8. Add unit tests.
9. Compare output with the current Ubuntu optimizer script.

### 22. Guiding principle
DRO should prefer a safe and explainable decision over aggressive optimization.

Every automatic DNS change must answer:
```text
What changed?
Why?
Which benchmark caused it?
How much better was the new IP?
What was the previous IP?
Can it be rolled back?
```

If DRO cannot answer those questions, it should not automatically change DNS.
