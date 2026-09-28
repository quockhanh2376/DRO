"""HTTPS benchmark runner using curl with hostname/SNI preserved."""

from __future__ import annotations

import os
import logging
import statistics
import subprocess
from typing import Callable, Sequence

from app.models.benchmark import BenchmarkResult, BenchmarkSample
from app.security import redact_secrets

logger = logging.getLogger(__name__)
REVOCATION_OFFLINE = "CRYPT_E_REVOCATION_OFFLINE"


def calculate_statistics(samples: Sequence[BenchmarkSample], requested_runs: int | None = None) -> BenchmarkResult:
    """Build aggregate statistics; jitter is population standard deviation of total time."""
    valid = [s for s in samples if s.valid]
    times = [float(s.total_ms) for s in valid]
    requested = requested_runs if requested_runs is not None else len(samples)
    healthy = len(valid) >= 3 and len(valid) >= 0.8 * requested
    failed = [sample for sample in samples if not sample.valid]
    reasons = [_sample_failure_reason(sample) for sample in failed]
    reason = None
    if not healthy and reasons:
        reason = max(dict.fromkeys(reasons), key=reasons.count)
    elif not healthy:
        reason = "Insufficient valid HTTPS samples"
    return BenchmarkResult(
        ip=samples[0].ip if samples else "", samples=list(samples), valid_runs=len(valid),
        requested_runs=requested, healthy=healthy,
        average_ms=statistics.fmean(times) if times else None,
        median_ms=statistics.median(times) if times else None,
        min_ms=min(times) if times else None, max_ms=max(times) if times else None,
        jitter_ms=(None if not times else 0.0 if len(times) == 1 else statistics.pstdev(times)),
        health_reason=reason,
    )


def _sample_failure_reason(sample: BenchmarkSample) -> str:
    if sample.http_status is not None and not 200 <= sample.http_status <= 399:
        return f"HTTP {sample.http_status} outside accepted range 200-399"
    error = (sample.error or "").casefold()
    if "certificate" in error or "ssl" in error or "tls" in error:
        return "TLS verification failed"
    if "timed out" in error or "timeout" in error:
        return "Connection timeout"
    if "connection refused" in error:
        return "Connection refused"
    if "connection reset" in error or "reset by peer" in error:
        return "Connection reset"
    if "could not resolve" in error or "resolve host" in error:
        return "Hostname/SNI resolution failed"
    return "No valid HTTPS response"


class HttpsBenchmarkRunner:
    def __init__(self, runs_per_ip: int = 10, timeout_seconds: float = 10.0,
                 command_runner: Callable[..., subprocess.CompletedProcess] | None = None):
        self.runs_per_ip = runs_per_ip
        self.timeout_seconds = timeout_seconds
        self._run = command_runner or subprocess.run

    def benchmark_ip(self, hostname: str, ip: str, path: str = "/", port: int = 443) -> BenchmarkResult:
        samples: list[BenchmarkSample] = []
        logger.info("Benchmark started host=%s ip=%s runs=%d", hostname, ip, self.runs_per_ip)
        for number in range(1, self.runs_per_ip + 1):
            command = self._command(hostname, ip, path, port)
            try:
                result = self._run(command, capture_output=True, text=True, timeout=self.timeout_seconds + 2, check=False)
                # Windows Schannel can lack access to a revocation endpoint in restricted environments.
                # Retry only this specific environmental error with revocation checking disabled; CA and
                # hostname verification remain on. All ordinary requests use curl's normal TLS settings.
                if os.name == "nt" and result.returncode and REVOCATION_OFFLINE in (result.stderr or ""):
                    logger.warning("Schannel revocation endpoint unavailable host=%s ip=%s; retrying with revocation check disabled", hostname, ip)
                    result = self._run(self._command(hostname, ip, path, port, skip_revocation=True),
                                       capture_output=True, text=True, timeout=self.timeout_seconds + 2, check=False)
                fields = (result.stdout or "").strip().split("\t")
                if result.returncode != 0 or len(fields) != 4:
                    raise RuntimeError((result.stderr or "curl returned incomplete timing data").strip())
                status = int(fields[0])
                connect_ms = float(fields[1]) * 1000
                appconnect_ms = float(fields[2]) * 1000
                total_ms = float(fields[3]) * 1000
                sample = BenchmarkSample(ip=ip, run_number=number, http_status=status,
                                         connect_ms=connect_ms, tls_ms=max(0.0, appconnect_ms-connect_ms),
                                         total_ms=total_ms)
                if not sample.valid:
                    sample.error = (f"HTTP {status} outside accepted range 200-399"
                                    if not 200 <= status <= 399 else "Missing HTTPS timing data")
                    logger.info("Benchmark sample failed host=%s ip=%s run=%d reason=%s",
                                hostname, ip, number, _sample_failure_reason(sample))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                sample = BenchmarkSample(ip=ip, run_number=number, error=redact_secrets(exc))
                detail = _sample_failure_reason(sample)
                logger.info("Benchmark sample failed host=%s ip=%s run=%d reason=%s detail=%s",
                            hostname, ip, number, detail, redact_secrets(exc))
            samples.append(sample)
        aggregate = calculate_statistics(samples, self.runs_per_ip)
        logger.info("Benchmark finished host=%s ip=%s valid=%d/%d healthy=%s avg_ms=%s reason=%s",
                    hostname, ip, aggregate.valid_runs, aggregate.requested_runs, aggregate.healthy,
                    aggregate.average_ms, aggregate.health_reason or "healthy")
        if not aggregate.healthy:
            logger.warning("Unhealthy benchmark candidate host=%s ip=%s valid=%d/%d reason=%s",
                           hostname, ip, aggregate.valid_runs, aggregate.requested_runs,
                           aggregate.health_reason)
        return aggregate

    def _command(self, hostname: str, ip: str, path: str, port: int, skip_revocation: bool = False) -> list[str]:
        command = ["curl", "--silent", "--show-error", "--http1.1", "--no-keepalive",
                   "-H", "Connection: close", "--resolve", f"{hostname}:{port}:{ip}",
                   "--connect-timeout", str(self.timeout_seconds), "--max-time", str(self.timeout_seconds),
                   "--output", "NUL" if os.name == "nt" else "/dev/null",
                   "--write-out", "%{http_code}\t%{time_connect}\t%{time_appconnect}\t%{time_total}"]
        if skip_revocation:
            command.extend(["--ssl-no-revoke"])
        command.append(f"https://{hostname}{path}")
        return command

    def benchmark(self, hostname: str, ips: Sequence[str], path: str = "/", port: int = 443) -> list[BenchmarkResult]:
        return [self.benchmark_ip(hostname, ip, path, port) for ip in ips]
