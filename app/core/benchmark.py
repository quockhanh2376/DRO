"""HTTPS benchmark runner using curl with hostname/SNI preserved."""

from __future__ import annotations

import os
import logging
import statistics
import subprocess
from typing import Callable, Sequence

from app.models.benchmark import BenchmarkResult, BenchmarkSample

logger = logging.getLogger(__name__)
REVOCATION_OFFLINE = "CRYPT_E_REVOCATION_OFFLINE"


def calculate_statistics(samples: Sequence[BenchmarkSample], requested_runs: int | None = None) -> BenchmarkResult:
    """Build aggregate statistics; jitter is population standard deviation of total time."""
    valid = [s for s in samples if s.valid]
    times = [float(s.total_ms) for s in valid]
    requested = requested_runs if requested_runs is not None else len(samples)
    healthy = len(valid) >= 3 and len(valid) >= 0.8 * requested
    return BenchmarkResult(
        ip=samples[0].ip if samples else "", samples=list(samples), valid_runs=len(valid),
        requested_runs=requested, healthy=healthy,
        average_ms=statistics.fmean(times) if times else None,
        median_ms=statistics.median(times) if times else None,
        min_ms=min(times) if times else None, max_ms=max(times) if times else None,
        jitter_ms=statistics.pstdev(times) if times else None,
    )


class HttpsBenchmarkRunner:
    def __init__(self, runs_per_ip: int = 10, timeout_seconds: float = 10.0,
                 command_runner: Callable[..., subprocess.CompletedProcess] | None = None):
        self.runs_per_ip = runs_per_ip
        self.timeout_seconds = timeout_seconds
        self._run = command_runner or subprocess.run

    def benchmark_ip(self, hostname: str, ip: str, path: str = "/", port: int = 443) -> BenchmarkResult:
        samples: list[BenchmarkSample] = []
        url = f"https://{hostname}{path}"
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
                    sample.error = "HTTP status outside accepted range 200-399 or missing timings"
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                sample = BenchmarkSample(ip=ip, run_number=number, error=str(exc))
                logger.debug("Benchmark run failed host=%s ip=%s run=%d error=%s", hostname, ip, number, exc)
            samples.append(sample)
        aggregate = calculate_statistics(samples, self.runs_per_ip)
        logger.info("Benchmark finished host=%s ip=%s valid=%d/%d healthy=%s avg_ms=%s",
                    hostname, ip, aggregate.valid_runs, aggregate.requested_runs, aggregate.healthy, aggregate.average_ms)
        if not aggregate.healthy:
            logger.warning("Unhealthy benchmark candidate host=%s ip=%s valid=%d/%d",
                           hostname, ip, aggregate.valid_runs, aggregate.requested_runs)
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
