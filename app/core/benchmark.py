"""HTTPS benchmark runner using curl with hostname/SNI preserved."""

from __future__ import annotations

import http.client
import logging
import os
import socket
import ssl
import statistics
import subprocess
import threading
import time
from collections import Counter
from typing import Callable, Literal, Sequence

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
    counts = Counter(reasons)
    reason = "; ".join(f"{count}x {label}" for label, count in counts.most_common()) if not healthy and counts else None
    if not healthy and not reason:
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
        if sample.http_status in (301, 302, 303, 307, 308):
            return "redirect failure"
        return f"HTTP {sample.http_status}"
    error = (sample.error or "").casefold()
    if "timed out" in error or "timeout" in error:
        if any(token in error for token in ("while reading", "read timeout", "response timeout", "receiving data")):
            return "read timeout"
        if any(token in error for token in ("failed to connect", "connect timeout", "operation timed out")):
            return "connect timeout"
        return "read timeout"
    if "connection refused" in error:
        return "connection refused"
    if "connection reset" in error or "reset by peer" in error:
        return "connection reset"
    if any(token in error for token in ("no alternative certificate", "hostname mismatch", "hostname/sni mismatch",
                                        "does not match", "subject name")):
        return "hostname/SNI mismatch"
    if any(token in error for token in ("certificate", "cert verify", "unknown ca", "self-signed")):
        return "TLS certificate verification failure"
    if any(token in error for token in ("could not resolve", "resolve host", "name or service not known", "temporary failure in name resolution")):
        return "DNS/resolve issue"
    if "redirect" in error or "too many redirects" in error:
        return "redirect failure"
    if "http" in error or "curl:" in error:
        return "other HTTP/client error"
    return "other HTTP/client error"


class HttpsBenchmarkRunner:
    def __init__(self, runs_per_ip: int = 10, timeout_seconds: float = 10.0,
                 command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
                 client: Literal["curl", "native"] = "curl",
                 ssl_context_factory: Callable[[], ssl.SSLContext] | None = None):
        self.runs_per_ip = runs_per_ip
        self.timeout_seconds = timeout_seconds
        self._run = command_runner or subprocess.run
        if client not in {"curl", "native"}:
            raise ValueError("client must be 'curl' or 'native'")
        self.client = client
        self._ssl_context_factory = ssl_context_factory or ssl.create_default_context

    def benchmark_ip(self, hostname: str, ip: str, path: str = "/", port: int = 443) -> BenchmarkResult:
        samples: list[BenchmarkSample] = []
        logger.info("Benchmark started host=%s ip=%s runs=%d", hostname, ip, self.runs_per_ip)
        for number in range(1, self.runs_per_ip + 1):
            try:
                if self.client == "native":
                    sample = self._native_sample(hostname, ip, path, port, number)
                else:
                    command = self._command(hostname, ip, path, port)
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
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                sample = BenchmarkSample(ip=ip, run_number=number, error=redact_secrets(exc))
                detail = _sample_failure_reason(sample)
                logger.debug("Benchmark sample failed host=%s ip=%s run=%d category=%s",
                             hostname, ip, number, detail)
            samples.append(sample)
        aggregate = calculate_statistics(samples, self.runs_per_ip)
        logger.info("Benchmark finished host=%s ip=%s valid=%d/%d healthy=%s avg_ms=%s failure_summary=%s",
                    hostname, ip, aggregate.valid_runs, aggregate.requested_runs, aggregate.healthy,
                    aggregate.average_ms, aggregate.health_reason or "none")
        if not aggregate.healthy:
            logger.warning("Unhealthy benchmark candidate host=%s ip=%s valid=%d/%d healthy=%s failure_summary=%s",
                           hostname, ip, aggregate.valid_runs, aggregate.requested_runs,
                           aggregate.healthy, aggregate.health_reason)
        return aggregate

    def _native_sample(self, hostname: str, ip: str, path: str, port: int,
                       number: int) -> BenchmarkSample:
        """One fresh IP-pinned HTTP/1.1 request with curl-equivalent TLS identity checks."""
        started = time.perf_counter()
        deadline = started + self.timeout_seconds
        sock: socket.socket | None = None
        tls_sock: ssl.SSLSocket | None = None
        connection: _PinnedHTTPSConnection | None = None
        response: http.client.HTTPResponse | None = None
        watchdog: threading.Timer | None = None
        transport = {"socket": None, "expired": False}
        transport_lock = threading.Lock()
        phase = "connect"
        try:
            remaining = max(0.001, deadline - time.perf_counter())
            sock = socket.create_connection((ip, port), timeout=remaining)
            connected = time.perf_counter()
            transport["socket"] = sock
            watchdog = threading.Timer(max(0.001, deadline - time.perf_counter()),
                                       _expire_native_transport, args=(transport, transport_lock))
            watchdog.daemon = True
            watchdog.start()
            phase = "TLS"
            context = self._ssl_context_factory()
            _set_deadline_timeout(sock, deadline)
            tls_sock = context.wrap_socket(sock, server_hostname=hostname)
            sock = None  # TLS socket now owns the underlying socket.
            with transport_lock:
                transport["socket"] = tls_sock
                expired = transport["expired"]
            if expired:
                raise TimeoutError("HTTPS request deadline exceeded")
            secured = time.perf_counter()
            connection = _PinnedHTTPSConnection(hostname, port, context, self.timeout_seconds,
                                                deadline=deadline, connected_socket=tls_sock)
            phase = "request"
            request_path = path if path.startswith("/") else "/" + path
            connection.putrequest("GET", request_path, skip_host=True)
            host_header = hostname if port == 443 else f"{hostname}:{port}"
            connection.putheader("Host", host_header)
            connection.putheader("Accept", "*/*")
            connection.putheader("Connection", "close")
            connection.endheaders()
            response = http.client.HTTPResponse(tls_sock, method="GET")
            phase = "read"
            _set_deadline_timeout(tls_sock, deadline)
            response.begin()
            status = response.status
            # Match curl's --output /dev/null: transfer the body before stopping total time.
            response_sock = tls_sock
            while True:
                phase = "read"
                _set_deadline_timeout(response_sock, deadline)
                if not response.read1(65536):
                    break
            total_ms = (time.perf_counter() - started) * 1000
            sample = BenchmarkSample(ip=ip, run_number=number, http_status=status,
                                     connect_ms=(connected - started) * 1000,
                                     tls_ms=(secured - connected) * 1000, total_ms=total_ms)
            if not sample.valid:
                sample.error = (f"HTTP {status} outside accepted range 200-399"
                                if not 200 <= status <= 399 else "Missing HTTPS timing data")
            connection.close()
            return sample
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
            return BenchmarkSample(ip=ip, run_number=number,
                                   error=_native_failure_detail(exc, phase, deadline))
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            if tls_sock is not None:
                tls_sock.close()
            if sock is not None:
                sock.close()

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


def _set_deadline_timeout(sock: socket.socket | ssl.SSLSocket | None, deadline: float) -> None:
    if sock is None:
        raise TimeoutError("HTTPS request socket is closed")
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise TimeoutError("HTTPS request deadline exceeded")
    sock.settimeout(remaining)


def _native_failure_detail(exc: Exception, phase: str, deadline: float) -> str:
    if time.perf_counter() >= deadline or isinstance(exc, TimeoutError):
        return "connect timeout" if phase in {"connect", "TLS"} else "read timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)) or getattr(exc, "winerror", None) in {10053, 10054}:
        return "connection reset"
    if isinstance(exc, socket.gaierror):
        return "DNS/resolve issue"
    if isinstance(exc, ssl.SSLError):
        message = str(exc).casefold()
        if any(token in message for token in ("hostname mismatch", "does not match", "not valid for", "subject name")):
            return "hostname/SNI mismatch"
        if isinstance(exc, ssl.SSLCertVerificationError) or any(
                token in message for token in ("certificate", "cert verify", "unknown ca", "self-signed")):
            return "TLS certificate verification failure"
    return redact_secrets(exc)


def _expire_native_transport(transport: dict, lock: threading.Lock) -> None:
    with lock:
        transport["expired"] = True
        sock = transport["socket"]
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """stdlib HTTP handling over a socket pinned to an IP, retaining hostname TLS identity."""
    def __init__(self, hostname: str, port: int, context: ssl.SSLContext,
                 timeout: float, *, deadline: float, connected_socket: ssl.SSLSocket):
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._deadline = deadline
        self._connected_socket = connected_socket

    def connect(self) -> None:
        self.sock = self._connected_socket
        _set_deadline_timeout(self.sock, self._deadline)

    def send(self, data) -> None:
        if self.sock is None:
            self.connect()
        _set_deadline_timeout(self.sock, self._deadline)
        super().send(data)
