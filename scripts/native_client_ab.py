"""Five-repeat, fixed-candidate curl/native benchmark comparison (read-only)."""

from __future__ import annotations

import json
import logging
import os
import statistics
import sys
import time
import subprocess
import threading
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor

from app.core.benchmark import HttpsBenchmarkRunner
from app.core.decision import rank_candidates
from app.core.discovery import PublicDnsDiscovery


PLANS = {
    "A": [("example.com", 2)],
    "B": [("www.google.com", 4)],
    "C": [("example.com", 2), ("www.google.com", 2)],
}


def _windows_child_usage(handle):
    """Read child curl CPU and peak working set while its process handle is open."""
    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    cpu_ms = peak_rss = None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                         ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    if kernel32.GetProcessTimes(wintypes.HANDLE(handle), ctypes.byref(created), ctypes.byref(exited),
                                ctypes.byref(kernel), ctypes.byref(user)):
        ticks = lambda value: (value.high << 32) | value.low
        cpu_ms = (ticks(kernel) + ticks(user)) / 10000
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    if psapi.GetProcessMemoryInfo(wintypes.HANDLE(handle), ctypes.byref(counters), counters.cb):
        peak_rss = counters.PeakWorkingSetSize
    return cpu_ms, peak_rss


def _measured_curl(command, metrics, lock, **kwargs):
    """subprocess.run-compatible invocation with Windows child CPU/RSS accounting."""
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=kwargs.get("text", False)) as process:
        try:
            stdout, stderr = process.communicate(timeout=kwargs.get("timeout"))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        if os.name == "nt":
            cpu_ms, peak_rss = _windows_child_usage(process._handle)
            with lock:
                if cpu_ms is not None:
                    metrics["curl_child_cpu_ms"] += cpu_ms
                if peak_rss is not None:
                    metrics["curl_child_peak_rss_bytes"] = max(metrics["curl_child_peak_rss_bytes"], peak_rss)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _windows_parent_memory():
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentProcess()
    if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return counters.WorkingSetSize, counters.PeakWorkingSetSize
    return None, None


def discover_candidates():
    discovery = PublicDnsDiscovery()
    try:
        result = {}
        for hostname in {host for plan in PLANS.values() for host, _ in plan}:
            result[hostname] = discovery.discover(hostname)
        return result
    finally:
        discovery.close()


def run_target(hostname, ips, client, resource_metrics, resource_lock):
    runner = HttpsBenchmarkRunner(runs_per_ip=10, timeout_seconds=10, client=client,
                                  command_runner=(lambda command, **kwargs: _measured_curl(
                                      command, resource_metrics, resource_lock, **kwargs)) if client == "curl" else None)
    started = time.perf_counter()
    candidates = [runner.benchmark_ip(hostname, ip) for ip in ips]
    elapsed = (time.perf_counter() - started) * 1000
    valid = sum(candidate.valid_runs for candidate in candidates)
    samples = sum(candidate.requested_runs for candidate in candidates)
    ranked = rank_candidates(candidates)
    return {"hostname": hostname, "candidate_wall_ms": round(elapsed, 3),
            "samples": samples, "valid": valid, "failed": samples - valid,
            "candidates": [result.model_dump(exclude={"samples"}) | {
                "sample_timings": [{"http_status": sample.http_status, "connect_ms": sample.connect_ms,
                                    "tls_ms": sample.tls_ms, "total_ms": sample.total_ms,
                                    "error": sample.error} for sample in result.samples],
                "failure_summary": result.health_reason,
            } for result in candidates],
            "ranked_ips": [result.ip for result in ranked]}


def run_workload(case, client, discovered):
    plans = [(hostname, discovered[hostname][:count])
             for hostname, count in PLANS[case]]
    for hostname, ips in plans:
        if len(ips) < next(count for host, count in PLANS[case] if host == hostname):
            raise RuntimeError(f"{hostname} did not resolve enough candidates: {ips}")
    started = time.perf_counter()
    cpu_started = time.process_time()
    resource_metrics = {"curl_child_cpu_ms": 0.0, "curl_child_peak_rss_bytes": 0}
    resource_lock = threading.Lock()
    print(f"Case {case}: starting client={client}", file=sys.stderr, flush=True)
    if case == "C":
        with ThreadPoolExecutor(max_workers=2) as pool:
            targets = list(pool.map(lambda plan: run_target(*plan, client, resource_metrics, resource_lock), plans))
    else:
        targets = [run_target(*plans[0], client, resource_metrics, resource_lock)]
    parent_rss, parent_peak_rss = _windows_parent_memory() if os.name == "nt" else (None, None)
    return {"batch_wall_ms": round((time.perf_counter() - started) * 1000, 3),
            "process_cpu_ms": round((time.process_time() - cpu_started) * 1000, 3),
            "curl_child_cpu_ms": round(resource_metrics["curl_child_cpu_ms"], 3),
            "curl_child_peak_rss_bytes": resource_metrics["curl_child_peak_rss_bytes"],
            "parent_rss_bytes": parent_rss, "parent_peak_rss_bytes": parent_peak_rss,
            "targets": targets}


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    discovered = discover_candidates()
    cases = {}
    clients = ("curl", "native")
    for case_index, case in enumerate(("A", "B", "C")):
        paired = {client: [] for client in clients}
        for repeat in range(5):
            order = clients if (repeat + case_index) % 2 == 0 else tuple(reversed(clients))
            for client in order:
                paired[client].append(run_workload(case, client, discovered))
                print(f"Case {case}: completed repeat={repeat + 1}/5 client={client}",
                      file=sys.stderr, flush=True)
        cases[case] = {}
        for client, observations in paired.items():
            flat_targets = [target for observation in observations for target in observation["targets"]]
            valid = sum(target["valid"] for target in flat_targets)
            total = sum(target["samples"] for target in flat_targets)
            cases[case][client] = {
                "batch_wall_median_ms": round(statistics.median(o["batch_wall_ms"] for o in observations), 3),
                "candidate_wall_median_ms_per_target": round(statistics.median(
                    target["candidate_wall_ms"] for target in flat_targets), 3),
                "valid_samples": valid, "failed_samples": total - valid,
                "samples": total, "process_cpu_ms": round(sum(
                    observation.get("process_cpu_ms", 0) for observation in observations), 3),
                "curl_child_cpu_ms": round(sum(observation["curl_child_cpu_ms"] for observation in observations), 3),
                "curl_child_peak_rss_bytes_max": max((observation["curl_child_peak_rss_bytes"]
                                                        for observation in observations), default=0),
                "parent_peak_rss_bytes_max": max((observation["parent_peak_rss_bytes"] or 0
                                                    for observation in observations), default=0),
                "observations": observations,
            }
    print(json.dumps({"python": sys.version.split()[0], "platform": sys.platform,
                      "repeats": 5, "runs_per_candidate": 10, "candidate_ips": discovered,
                      "cases": cases}, indent=2))


if __name__ == "__main__":
    main()
