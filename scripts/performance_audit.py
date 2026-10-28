"""Read-only performance measurements for DRO's benchmark path.

Examples:
    python scripts/performance_audit.py --mode deterministic --repeats 3
    python scripts/performance_audit.py --mode network --repeats 3

Network mode only queries public DNS-over-HTTPS and performs HTTPS GET requests
to discovered candidate IPs. It uses an in-memory SQLite database and never
calls a rewrite mutation endpoint.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core import benchmark as benchmark_module
from app.core.benchmark import HttpsBenchmarkRunner
from app.core.discovery import PublicDnsDiscovery
from app.core.optimizer import read_adguard_rewrite
from app.db.database import Database
from app.db.models import Base
from app.db.repositories import add_audit_event, save_benchmark_run, save_optimizer_state, save_target
from app.models.benchmark import BenchmarkSample, DecisionResult, PendingCandidateState
from app.models.target import Target


@dataclass
class Metrics:
    target: str
    total_wall_ms: float = 0.0
    discovery_ms: float = 0.0
    adguard_lookup_ms: float = 0.0
    adguard_lookup_succeeded: bool | None = None
    resolver_requests: list[dict[str, Any]] = field(default_factory=list)
    cname_ms: float = 0.0
    addresses_returned: int = 0
    candidate_count: int = 0
    runs_per_candidate: int = 0
    candidate_wall_ms: dict[str, float] = field(default_factory=dict)
    sample_count: int = 0
    valid_samples: int = 0
    failed_samples: int = 0
    subprocess_count: int = 0
    command_invocations: int = 0
    subprocess_wall_ms: float = 0.0
    curl_reported_total_ms: float = 0.0
    process_cpu_ms: float = 0.0
    rss_start_bytes: int | None = None
    rss_end_bytes: int | None = None
    peak_rss_bytes: int | None = None
    log_records: int = 0
    log_handler_ms: float = 0.0
    summarize_calls: int = 0
    summarize_ms: float = 0.0
    db_inserts: int = 0
    db_updates: int = 0
    db_deletes: int = 0
    db_write_ms: float = 0.0
    db_commits: int = 0
    db_commit_ms: float = 0.0


def process_usage() -> tuple[float, int | None, int | None]:
    """Return process CPU seconds, current RSS, and peak RSS without dependencies."""
    cpu = time.process_time()
    current = peak = None
    try:
        import psutil  # optional; not a DRO dependency
        info = psutil.Process().memory_info()
        current, peak = info.rss, getattr(info, "peak_wset", None) or info.rss
    except ImportError:
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

                counters = PROCESS_MEMORY_COUNTERS()
                counters.cb = ctypes.sizeof(counters)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                psapi = ctypes.WinDLL("psapi", use_last_error=True)
                kernel32.GetCurrentProcess.restype = wintypes.HANDLE
                psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                                       ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                                                       wintypes.DWORD]
                psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
                handle = kernel32.GetCurrentProcess()
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    current, peak = counters.WorkingSetSize, counters.PeakWorkingSetSize
            except (AttributeError, OSError):
                pass
        else:
            try:
                with open("/proc/self/statm", encoding="ascii") as statm:
                    current = int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
                import resource
                value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                peak = value if sys.platform == "darwin" else value * 1024
            except ImportError:
                pass
            except OSError:
                pass
    return cpu, current, peak


class TimedDnsClient:
    """Time DoH requests while forwarding the production client's behavior."""
    def __init__(self, metrics: Metrics):
        import httpx
        self._client = httpx.Client(timeout=5.0, follow_redirects=True)
        self.metrics = metrics

    def get(self, url: str, **kwargs):
        start = time.perf_counter()
        try:
            return self._client.get(url, **kwargs)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            params = kwargs.get("params", {})
            self.metrics.resolver_requests.append({"resolver": "dns.google", "name": params.get("name"),
                                                   "duration_ms": round(elapsed, 3)})

    def close(self):
        self._client.close()


class TimedDb:
    def __init__(self, metrics: Metrics):
        self.metrics = metrics
        self.db = Database("sqlite:///:memory:")
        Base.metadata.create_all(self.db.engine)
        self._starts = threading.local()
        self.targets: dict[str, int] = {}

    def prepare(self, hostname: str):
        """Seed an already-existing target/state outside the measured benchmark transaction."""
        with self.db.session() as session:
            target = save_target(session, Target(hostname=hostname))
            save_optimizer_state(session, target.id, None, PendingCandidateState(),
                                 DecisionResult(action="KEEP", reason="Seed current state"))
            self.targets[hostname] = target.id

    def start_measurement(self):
        event.listen(self.db.engine, "before_cursor_execute", self._before_sql)
        event.listen(self.db.engine, "after_cursor_execute", self._after_sql)
        event.listen(Session, "before_commit", self._before_commit)
        event.listen(Session, "after_commit", self._after_commit)

    def _before_sql(self, _conn, _cursor, statement, _params, _context, _many):
        sql = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else ""
        if sql in {"INSERT", "UPDATE", "DELETE"}:
            setattr(self._starts, "sql_start", (sql, time.perf_counter()))

    def _after_sql(self, _conn, _cursor, _statement, _params, _context, _many):
        started = getattr(self._starts, "sql_start", None)
        if not started:
            return
        sql, start = started
        self.metrics.db_write_ms += (time.perf_counter() - start) * 1000
        setattr(self.metrics, {"INSERT": "db_inserts", "UPDATE": "db_updates",
                               "DELETE": "db_deletes"}[sql],
                getattr(self.metrics, {"INSERT": "db_inserts", "UPDATE": "db_updates",
                                      "DELETE": "db_deletes"}[sql]) + 1)
        self._starts.sql_start = None

    def _before_commit(self, _session):
        if _session.get_bind() is not self.db.engine:
            return
        self._starts.commit_start = time.perf_counter()

    def _after_commit(self, _session):
        if _session.get_bind() is not self.db.engine:
            return
        started = getattr(self._starts, "commit_start", None)
        if started is not None:
            self.metrics.db_commits += 1
            self.metrics.db_commit_ms += (time.perf_counter() - started) * 1000

    def save(self, hostname: str, results):
        with self.db.session() as session:
            target_id = self.targets[hostname]
            save_benchmark_run(session, target_id, results,
                               {"candidate_count": len(results), "read_only_audit": True},
                               DecisionResult(action="KEEP", reason="Performance audit"))
            save_optimizer_state(session, target_id, None, PendingCandidateState(),
                                 DecisionResult(action="KEEP", reason="Performance audit"))
            add_audit_event(session, "benchmark_completed", target_id, {"candidate_count": len(results)})

    def close(self):
        event.remove(Session, "before_commit", self._before_commit)
        event.remove(Session, "after_commit", self._after_commit)
        self.db.close()


class TimedLogHandlers:
    """Measure real handler emits without suppressing or replacing operational logs."""
    def __init__(self):
        self.root = logging.getLogger()
        self.saved: list[tuple[logging.Handler, Any]] = []
        self.records = 0
        self.duration = 0.0

    def __enter__(self):
        if not self.root.handlers:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        for handler in self.root.handlers:
            original = handler.emit
            self.saved.append((handler, original))

            def timed(record, emit=original):
                start = time.perf_counter()
                try:
                    self.records += 1
                    return emit(record)
                finally:
                    self.duration += time.perf_counter() - start
            handler.emit = timed
        return self

    def __exit__(self, *_exc):
        for handler, original in self.saved:
            handler.emit = original


def _format_curl_result() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, "200\t0.001\t0.002\t0.003\n", "")


def run_target(hostname: str, requested_candidates: int, mode: str,
               requested_runs: int, db_write: bool = True) -> Metrics:
    metrics = Metrics(hostname)
    cpu_start, metrics.rss_start_bytes, _ = process_usage()
    target_started = time.perf_counter()

    if mode == "network":
        dns = TimedDnsClient(metrics)
        discovery = PublicDnsDiscovery(client=dns)
        discover_start = time.perf_counter()
        addresses = discovery.discover(hostname)
        metrics.discovery_ms = (time.perf_counter() - discover_start) * 1000
        metrics.cname_ms = sum(item["duration_ms"] for item in metrics.resolver_requests[1:])
        discovery.close()
        candidates = addresses[:requested_candidates]
        if len(candidates) < requested_candidates:
            raise RuntimeError(f"{hostname} resolved {len(candidates)} candidates; need {requested_candidates}")
    else:
        # Reserved documentation addresses make the sample/candidate workload stable.
        candidates = [f"192.0.2.{index + 1}" for index in range(requested_candidates)]
        metrics.discovery_ms = 0.0
        metrics.addresses_returned = len(candidates)

    metrics.addresses_returned = len(candidates)
    if mode == "network":
        read_started = time.perf_counter()
        _current_ip, metrics.adguard_lookup_succeeded = read_adguard_rewrite(hostname)
        metrics.adguard_lookup_ms = (time.perf_counter() - read_started) * 1000
    metrics.candidate_count = len(candidates)
    metrics.runs_per_candidate = requested_runs
    results = []
    stats_code = benchmark_module.calculate_statistics.__code__
    profile_stack: list[float] = []

    def profiler(frame, event_name, _arg):
        if frame.f_code is stats_code:
            if event_name == "call":
                profile_stack.append(time.perf_counter())
            elif event_name == "return" and profile_stack:
                metrics.summarize_calls += 1
                metrics.summarize_ms += (time.perf_counter() - profile_stack.pop()) * 1000

    previous_profiler = sys.getprofile()
    sys.setprofile(profiler)
    try:
        for ip in candidates:
            sample_start = time.perf_counter()
            if mode == "network":
                def measured_run(*args, **kwargs):
                    started = time.perf_counter()
                    metrics.subprocess_count += 1
                    metrics.command_invocations += 1
                    try:
                        return subprocess.run(*args, **kwargs)
                    finally:
                        metrics.subprocess_wall_ms += (time.perf_counter() - started) * 1000
                runner = HttpsBenchmarkRunner(requested_runs, timeout_seconds=10,
                                               command_runner=measured_run)
            else:
                def fake_subprocess(*_args, **_kwargs):
                    metrics.command_invocations += 1
                    return _format_curl_result()
                runner = HttpsBenchmarkRunner(requested_runs, timeout_seconds=10,
                                               command_runner=fake_subprocess)
            result = runner.benchmark_ip(hostname, ip)
            metrics.candidate_wall_ms[ip] = (time.perf_counter() - sample_start) * 1000
            metrics.sample_count += len(result.samples)
            metrics.valid_samples += result.valid_runs
            metrics.failed_samples += len(result.samples) - result.valid_runs
            metrics.curl_reported_total_ms += sum(sample.total_ms or 0 for sample in result.samples)
            results.append(result)
    finally:
        sys.setprofile(previous_profiler)

    if db_write:
        persistence = TimedDb(metrics)
        try:
            persistence.prepare(hostname)
            persistence.start_measurement()
            persistence.save(hostname, results)
        finally:
            persistence.close()
    metrics.total_wall_ms = (time.perf_counter() - target_started) * 1000
    metrics.process_cpu_ms = (time.process_time() - cpu_start) * 1000
    _, metrics.rss_end_bytes, metrics.peak_rss_bytes = process_usage()
    if mode == "network":
        metrics.subprocess_wall_ms = round(metrics.subprocess_wall_ms, 3)
    return metrics


def run_case(case: str, repeats: int, mode: str, runs: int) -> dict[str, Any]:
    if case == "A":
        plan = [("example.com", 2)]
    elif case == "B":
        plan = [("www.google.com", 4)]
    else:
        plan = [("example.com", 2), ("www.google.com", 2)]
    observations = []
    cpu_start = time.process_time()
    with TimedLogHandlers() as logs:
        for rep in range(repeats):
            started = time.perf_counter()
            if case == "C":
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(run_target, host, count, mode, runs)
                               for host, count in plan]
                    target_metrics = [future.result() for future in futures]
            else:
                target_metrics = [run_target(plan[0][0], plan[0][1], mode, runs)]
            wall_ms = (time.perf_counter() - started) * 1000
            observations.append({"repeat": rep + 1, "batch_wall_ms": wall_ms,
                                 "targets": [asdict(metric) for metric in target_metrics]})
    batch_times = [item["batch_wall_ms"] for item in observations]
    flattened = [target for item in observations for target in item["targets"]]
    sums = lambda field: sum(target[field] for target in flattened)
    candidate_wall_medians = [sum(target["candidate_wall_ms"].values())
                              for target in flattened]
    return {"case": case, "mode": mode, "repeats": repeats,
            "batch_wall_ms_median": median(batch_times),
            "target_wall_ms_median": median([target["total_wall_ms"] for target in flattened]),
            "benchmark_candidate_wall_ms_median": median(candidate_wall_medians),
            "discovery_ms_median": median([target["discovery_ms"] for target in flattened]),
            "adguard_lookup_ms_median": median([target["adguard_lookup_ms"] for target in flattened]),
            "subprocess_count_median_per_repeat": median(
                sum(target["subprocess_count"] for target in item["targets"]) for item in observations),
            "command_invocations_median_per_repeat": median(
                sum(target["command_invocations"] for target in item["targets"]) for item in observations),
            "subprocess_wall_ms_total": round(sums("subprocess_wall_ms"), 3),
            "curl_reported_total_ms": round(sums("curl_reported_total_ms"), 3),
            "subprocess_minus_curl_estimated_overhead_ms": (round(
                sums("subprocess_wall_ms") - sums("curl_reported_total_ms"), 3)
                if mode == "network" else None),
            "candidate_count_per_target": [target["candidate_count"] for target in flattened],
            "sample_count_total": sums("sample_count"), "valid_samples_total": sums("valid_samples"),
            "failed_samples_total": sums("failed_samples"),
            "process_cpu_ms_total": round((time.process_time() - cpu_start) * 1000, 3),
            "peak_rss_bytes_max": max((target["peak_rss_bytes"] or 0 for target in flattened), default=0),
            "log_records_total": logs.records, "log_handler_ms_total": round(logs.duration * 1000, 3),
            "db_inserts_total": sums("db_inserts"), "db_updates_total": sums("db_updates"),
            "db_deletes_total": sums("db_deletes"), "db_write_ms_total": round(sums("db_write_ms"), 3),
            "db_commits_total": sums("db_commits"), "db_commit_ms_total": round(sums("db_commit_ms"), 3),
            "summarize_calls_total": sums("summarize_calls"),
            "summarize_ms_total": round(sums("summarize_ms"), 3),
            "observations": observations}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("deterministic", "network"), default="deterministic")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--case", choices=("A", "B", "C", "all"), default="all")
    args = parser.parse_args()
    if min(args.repeats, args.runs) < 1:
        parser.error("--repeats and --runs must be positive")
    results = [run_case(case, args.repeats, args.mode, args.runs)
               for case in (("A", "B", "C") if args.case == "all" else (args.case,))]
    print(json.dumps({"python": sys.version.split()[0], "platform": sys.platform,
                      "concurrency_limit": 2, "mode": args.mode, "cases": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
