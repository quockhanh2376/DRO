# DRO performance audit

Audit date: 2026-09-29  
Repository baseline: `1247511` (the current application code at audit start)  
Measurement host: Windows, Python 3.12.10, system `curl.exe`; no `psutil` installed.  
Scope: read-only public DNS-over-HTTPS and HTTPS candidate requests; benchmark persistence was measured against disposable in-memory SQLite. No production DNS, production database, or rewrite endpoint was modified.

## Current architecture

| Area | Current implementation |
| --- | --- |
| Target concurrency | `RunCoordinator` defaults to at most two active targets. A condition lock protects its active set and FIFO queue. |
| Candidate concurrency | Sequential within a target (`HttpsBenchmarkRunner.benchmark` calls `benchmark_ip` in input order). |
| Samples per candidate | Sequential loop; each sample is a new curl process and a new HTTPS request. |
| Queue and duplicates | Manual web Run Now submits to the FIFO coordinator. Repeating an active/queued target returns its existing state instead of scheduling another copy. Queue tests exercise two active targets, FIFO release, and duplicate prevention. |
| Scheduler interaction | The single scheduler worker processes due IDs in order and uses `coordinator.run`, sharing the same per-process limit/duplicate guard with manual and API runs. A target already active is skipped for that scheduler cycle. |
| Subprocesses | `benchmark.py` invokes `subprocess.run` once per sample. On Windows, the specific Schannel revocation-offline error can cause an additional retry process. The curl invocation pins the candidate with `--resolve`, retains URL hostname/SNI and normal certificate/hostname verification, and requests `Connection: close`. |
| HTTP reuse | DoH uses one `httpx.Client` per discovery object, reused for sequential CNAME queries. Benchmark HTTPS has no client pool: each sample starts curl and a fresh connection. AdGuard clients are created and closed for each read. |
| DNS discovery | One resolver endpoint (`https://dns.google/resolve`). CNAMEs are followed sequentially from a queue, with deduplication, cycle protection, and an eight-hop bound. No per-resolver fan-out exists today. |
| Logging | Standard Python logging configured with `basicConfig`; persistent stream handlers emit to stderr. Under systemd, stderr is appended to the configured log file descriptor. Application code does not open/close a log file for each record and has no queue listener. |
| Benchmark DB writes | Samples/results/run/state/audit are added/flushed inside the benchmark transaction. There are no per-sample commits. The outer API/web/session boundary commits; scheduler separates benchmark persistence from automatic rewrite processing. |
| Statistics | `calculate_statistics` filters valid samples, builds failure counts, then calculates fmean, median, min, max, and population standard deviation. With 10–15 samples the aggregation is a few small passes. |
| Synchronization | `RunCoordinator` uses `threading.Condition`. Live ping has its own bounded session tracking. The coordinator is process-local and assumes the current single DRO worker process. |

## Instrumentation and reproduction

The new `scripts/performance_audit.py` offers two modes:

- Deterministic: uses reserved documentation IPs and a fake curl runner. It measures runner/statistics/SQLite/logging overhead without external network requests; reported real subprocess count is zero and command invocations are counted separately.
- Network: runs three repeats of the requested production-shaped workloads using the current public DoH resolver and the existing curl runner. It also performs a read-only AdGuard lookup and records its duration/outcome. All persistence uses a fresh in-memory SQLite database with an already-existing target/state seeded before timing.

Reproduce the network baseline with:

```powershell
python scripts/performance_audit.py --mode network --repeats 3 --runs 10 --case all
```

The JSON includes each repetition, hostname/IP, resolver request time, CNAME work, candidate times, sample validity, curl process count/wall time, curl-reported request time, process CPU/RSS, root log handler record/time totals, SQL write/commit counts and durations, and per-call statistics time. The run uses `example.com` for two IPs, `www.google.com` for four IPs, and those two targets concurrently for Case C. Network answers/timings vary; the script fails clearly if a domain no longer provides the requested candidate count.

Case C uses a bounded two-worker executor to model the production target limit. Separate existing tests cover the actual `RunCoordinator` queue/FIFO/duplicate behavior.

## Baseline measurements

Each case ran three times. Wall figures below are medians; cumulative counters cover all three repetitions. Case C reports two targets per repetition. “Candidate work” sums measured per-candidate `benchmark_ip` wall times for a target.

| Metric | Case A: 1 target, 2 IPs × 10 | Case B: 1 target, 4 IPs × 10 | Case C: 2 targets concurrent, 2 IPs × 10 each |
| --- | ---: | ---: | ---: |
| Batch wall time, median | 9,121 ms | 18,144 ms | 11,133 ms |
| Target wall time, median | 9,119 ms | 18,140 ms | 10,635 ms |
| Candidate benchmark work, median / target | 5,766 ms | 14,463 ms | 7,094 ms |
| DNS discovery, median / target | 158 ms | 115 ms | 148 ms |
| AdGuard read, median / target | 2,678 ms | 2,499 ms | 2,735 ms |
| Resolver requests | 1 × `dns.google` | 1 × `dns.google` | 1 × `dns.google` per target |
| CNAME traversal | 0 ms; no CNAME in these runs | 0 ms; no CNAME in these runs | 0 ms; no CNAME in these runs |
| Actual curl subprocesses / repeat | 20 | 40 | 40 batch; 20 / target |
| Samples / repeat; valid | 20; 20 | 40; 40 | 40; 40 |
| Curl-reported request total / 3 repeats | 13,902 ms | 36,340 ms | 32,296 ms |
| `subprocess.run` wall / 3 repeats | 17,321 ms | 43,845 ms | 41,289 ms |
| Outer-minus-curl timing estimate / sample | 57 ms | 63 ms | 75 ms |
| Parent-process CPU / three repeats | 4,234 ms | 4,141 ms | 8,469 ms |
| Peak RSS | 74.9 MiB | 75.7 MiB | 80.7 MiB |
| Log records / three repeats | 24 | 36 | 48 |
| Time inside existing log handlers / three repeats | 1.67 ms | 2.52 ms | 5.19 ms |
| SQL INSERT / UPDATE / DELETE statements / three repeats | 72 / 3 / 0 | 138 / 3 / 0 | 144 / 6 / 0 |
| SQL write time / three repeats | 1.65 ms | 4.07 ms | 7.79 ms |
| Commits / time / three repeats | 3 / 0.12 ms | 3 / 0.12 ms | 6 / 0.31 ms |
| `calculate_statistics` calls / time / three repeats | 6 / 1.65 ms | 12 / 3.08 ms | 12 / 3.23 ms |

All 300 real HTTPS samples were valid in this measurement window; no revocation retries occurred. Curl time above is the request timing reported by curl. The difference from `subprocess.run` wall time includes process launch/exit and wrapper overhead, so it is an estimate of process overhead, not a pure OS process-creation microbenchmark.

The local workstation has no AdGuard endpoint configured/discoverable. The read-only lookup therefore failed closed and took 2.50–2.74 seconds per target in these runs. This is a measured cost of the local configuration path; it is not representative of a reachable production AdGuard API. The baseline does not use a cached rewrite as a substitute. Production benchmark timing with a configured AdGuard endpoint will have a different preflight cost.

RSS uses Windows `GetProcessMemoryInfo` because `psutil` is absent; the utility supports optional `psutil` or platform resource counters elsewhere. CPU is the Python audit process CPU time, not aggregate CPU for child curl processes.

## Bottleneck classification

1. **HIGH — HTTPS candidate/sample work and external network latency.** Candidate benchmark work ranges from 5.8 seconds to 14.5 seconds per target. Curl reports about 232–303 ms per sample in these runs. Case B grows almost linearly with its 2× candidate/sample count. The network dominates.
2. **MEDIUM, environment-specific — AdGuard discovery/read failure.** The local no-endpoint path costs 2.5–2.7 seconds per target. A configured production endpoint should avoid endpoint discovery and return the rewrite list directly; measure that host separately before any AdGuard-path change.
3. **MEDIUM — curl process overhead.** The measured outer-minus-curl delta is about 57–75 ms/sample, roughly 13–25% of the reported request time depending on the case. Across 10 samples/IP this accumulates, but replacing the process boundary has material semantic and error-attribution risks.

Other observations:

- DNS is one resolver request and roughly 115–158 ms, under 2% of observed target wall time. It is not a sequential multi-resolver bottleneck; CNAME traversal is inherently dependent and was absent from these runs.
- Two-target concurrency gives about 11.1 seconds batch wall for two roughly 10.6 second target runs, consistent with the configured limit. No candidate/run-level parallelism is used. More concurrency risks measurement contention.
- Logging is persistent-handler, synchronous logging. Handler emit time is only 1.7–5.2 ms across the three-repeat cases; no logging optimization is justified.
- SQLite persists each sample as a row, plus aggregate/run/audit/state data, but batches them into one commit per target run. Write/commit time remains under a few milliseconds per target. There are no per-sample commits.
- Statistics aggregation takes about 0.26–0.28 ms/call. Do not optimize it.

## Decisions and before/after

No application runtime optimization was accepted. The audit changed only by adding this utility, its deterministic instrumentation test, and documentation; benchmark semantics and throughput code remain unchanged. Since there was no runtime optimization, an “after” network run is not applicable and the measurements above are the measured baseline, not a fabricated before/after delta.

| Metric | Before (measured) | After | Decision |
| --- | --- | --- | --- |
| Total wall time | A 9.12 s; B 18.14 s; C 11.13 s batch median | Not applicable | No runtime change |
| DNS discovery | 115–158 ms median | Not applicable | Keep one resolver; already one request |
| Candidate benchmark work | 5.77–14.46 s / target median | Not applicable | Network latency dominates |
| Curl subprocesses | 20 / 40 / 40 per repeat | Not applicable | Retain exact per-sample semantics |
| CPU / peak RSS | 4.14–8.47 s CPU over three repeats; 74.9–80.7 MiB peak | Not applicable | No CPU/memory hotspot identified |
| Logging | 1.67–5.19 ms handler time over three repeats | Not applicable | Keep persistent handlers and operational records |
| DB writes | 24 inserts + 1 update and 1 commit per target (A); 46 inserts + 1 update and 1 commit (B) | Not applicable | Keep transactional sample persistence |
| `summarize()` | 0.26–0.28 ms per call | Not applicable | Too small to justify rewrite |

### Deliberately rejected

- Candidate-level parallelism: shorter wall time is not worth benchmark contention that can distort candidate latency.
- Curl-to-HTTPX replacement: a safe fixed-IP transport preserving original-host SNI, hostname validation, timeout behavior, status/redirect policy, and current error categories would need lower-level or fragile transport customization.
- Curl batching/process reuse: it could reduce the measured launch delta but complicates per-sample failure attribution, per-sample timeout behavior, and the Windows-only Schannel retry. Every request intentionally uses a fresh connection. No change was made without an equivalence proof.
- Concurrent DNS resolvers: there is only one configured resolver today, so there is no sequential resolver fan-out to parallelize. Changing resolver policy would be a reliability/product choice, not a measured speed optimization.
- Queue logging, DB transaction changes, and statistics rewrites: measured cost is negligible or already batched.

## Correctness and tests

Existing tests cover statistics and benchmark health, DoH CNAME/deduplication behavior, target concurrency/FIFO/queued duplicate behavior, scheduler/manual run guards, and persistence. A deterministic instrumentation regression checks sample/subprocess counts, database statements/commit, and aggregation call counts without asserting wall-clock thresholds.

Full pytest is run after adding the instrumentation and docs. No production validation or DNS operation is part of this local performance baseline.
