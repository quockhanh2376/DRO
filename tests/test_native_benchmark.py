from __future__ import annotations

import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core import benchmark as benchmark_module
from app.core.benchmark import HttpsBenchmarkRunner


FIXTURES = Path(__file__).parent / "fixtures" / "native_benchmark"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, status=200, body_delay=0, header_delay=0):
        self.status = status
        self.body_delay = body_delay
        self.header_delay = header_delay
        self.requests = []
        self.sni_names = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(FIXTURES / "server.pem", FIXTURES / "server-key.pem")
        context.set_servername_callback(lambda _sock, name, _ctx: self.sni_names.append(name))
        self.tls_context = context
        super().__init__(("127.0.0.1", 0), _Handler)

    def get_request(self):
        sock, address = super().get_request()
        return self.tls_context.wrap_socket(sock, server_side=True), address


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Host"), self.headers.get("Connection"),
                                     len(self.headers.get_all("Host", []))))
        if self.server.header_delay:
            for line in (b"HTTP/1.1 200 OK\r\n", b"Content-Length: 2\r\n", b"Connection: close\r\n", b"\r\n"):
                self.connection.sendall(line)
                time.sleep(self.server.header_delay)
            self.wfile.write(b"ok")
            return
        self.send_response(self.server.status)
        self.send_header("Content-Length", "2")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.server.body_delay:
            time.sleep(self.server.body_delay)
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        pass


@pytest.fixture
def https_server():
    server = _Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _trusted_context():
    return ssl.create_default_context(cafile=str(FIXTURES / "ca.pem"))


def _port(server):
    return server.server_address[1]


def test_curl_remains_default_and_preserves_timing_writeout():
    runner = HttpsBenchmarkRunner()
    command = runner._command("example.test", "192.0.2.10", "/health", 443)
    writeout = command[command.index("--write-out") + 1]
    assert runner.client == "curl"
    assert "--resolve" in command and "example.test:443:192.0.2.10" in command
    assert writeout == "%{http_code}\t%{time_connect}\t%{time_appconnect}\t%{time_total}"


def test_native_client_pins_ip_preserves_host_sni_tls_and_fresh_connection(https_server, monkeypatch):
    https_server.status = 302  # curl accepts 3xx and does not follow redirects.
    monkeypatch.setattr("subprocess.run", lambda *_a, **_kw: pytest.fail("native mode spawned curl"))
    runner = HttpsBenchmarkRunner(3, 2, client="native", ssl_context_factory=_trusted_context)
    result = runner.benchmark_ip("benchmark.test", "127.0.0.1", "/health?q=1", _port(https_server))

    assert result.healthy and result.valid_runs == 3, [sample.error for sample in result.samples]
    assert all(sample.connect_ms is not None and sample.tls_ms is not None and sample.total_ms is not None
               for sample in result.samples)
    assert https_server.sni_names == ["benchmark.test"] * 3
    assert https_server.requests == [("/health?q=1", f"benchmark.test:{_port(https_server)}", "close", 1)] * 3


def test_native_client_rejects_untrusted_certificate(https_server):
    result = HttpsBenchmarkRunner(1, 2, client="native").benchmark_ip(
        "benchmark.test", "127.0.0.1", port=_port(https_server))
    assert not result.healthy
    assert "TLS certificate verification failure" in result.health_reason


def test_native_client_verifies_hostname_separately_from_sni(https_server):
    result = HttpsBenchmarkRunner(1, 2, client="native", ssl_context_factory=_trusted_context).benchmark_ip(
        "other.test", "127.0.0.1", port=_port(https_server))
    assert not result.healthy
    assert "hostname/SNI mismatch" in result.health_reason, result.samples
    assert https_server.sni_names == ["other.test"]


def test_native_client_classifies_http_status_and_read_timeout(https_server):
    https_server.status = 403
    rejected = HttpsBenchmarkRunner(1, 2, client="native", ssl_context_factory=_trusted_context).benchmark_ip(
        "benchmark.test", "127.0.0.1", port=_port(https_server))
    assert not rejected.healthy and rejected.health_reason == "1x HTTP 403"

    https_server.status = 200
    https_server.body_delay = 0.15
    timed_out = HttpsBenchmarkRunner(1, 0.05, client="native", ssl_context_factory=_trusted_context).benchmark_ip(
        "benchmark.test", "127.0.0.1", port=_port(https_server))
    assert not timed_out.healthy
    assert timed_out.health_reason == "1x read timeout"


def test_native_client_enforces_total_deadline_while_headers_trickle(https_server):
    https_server.header_delay = 0.06
    started = time.perf_counter()
    timed_out = HttpsBenchmarkRunner(1, 0.1, client="native", ssl_context_factory=_trusted_context).benchmark_ip(
        "benchmark.test", "127.0.0.1", port=_port(https_server))
    elapsed = time.perf_counter() - started
    assert not timed_out.healthy
    assert timed_out.health_reason == "1x read timeout"
    assert elapsed < 0.5


@pytest.mark.parametrize(("exception", "expected"), [
    (ConnectionRefusedError("actively refused"), "1x connection refused"),
    (ConnectionResetError("forcibly closed"), "1x connection reset"),
])
def test_native_client_normalizes_socket_failure_categories(monkeypatch, exception, expected):
    def fail_connect(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(benchmark_module.socket, "create_connection", fail_connect)
    result = HttpsBenchmarkRunner(1, 1, client="native").benchmark_ip(
        "benchmark.test", "192.0.2.10")
    assert not result.healthy and result.health_reason == expected
