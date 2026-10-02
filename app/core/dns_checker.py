"""Small, read-only DNS diagnostic using a bounded set of public resolvers."""

from __future__ import annotations

import ipaddress
import secrets
import socket
import struct
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from typing import Any


@dataclass(frozen=True, slots=True)
class ResolverProfile:
    name: str
    region: str
    endpoint: str
    protocol: str = "UDP DNS"


RESOLVER_PROFILES = (
    ResolverProfile("Google", "Global / Anycast", "8.8.8.8:53"),
    ResolverProfile("Cloudflare", "Global / Anycast", "1.1.1.1:53"),
    ResolverProfile("Quad9", "Global / Anycast", "9.9.9.9:53"),
    ResolverProfile("OpenDNS", "Global / Anycast", "208.67.222.222:53"),
    ResolverProfile("VN Resolver", "Vietnam", "42.116.255.180:53"),
    ResolverProfile("SG Resolver", "Singapore", "129.126.119.238:53"),
    ResolverProfile("MY Resolver", "Malaysia", "1.9.63.98:53"),
    ResolverProfile("HK Resolver", "Hong Kong", "218.255.10.58:53"),
    ResolverProfile("AU Resolver", "Australia", "203.54.212.126:53"),
    ResolverProfile("JP Resolver", "Japan", "219.163.11.226:53"),
)
RECORD_TYPES = {"A": 1, "AAAA": 28, "CNAME": 5, "MX": 15, "TXT": 16}
RESOLVER_TIMEOUT_SECONDS = 3.0
MAX_RESOLVER_CONCURRENCY = 4


class DNSCheckInputError(ValueError):
    """Input cannot be used as a DNS hostname or record type."""


def normalize_hostname(value: str) -> str:
    if not isinstance(value, str):
        raise DNSCheckInputError("Enter a valid hostname.")
    raw = value.strip()
    if raw.endswith("."):
        raw = raw[:-1]
    if not raw or len(raw) > 253 or any(char in raw for char in "/\\:@?#"):
        raise DNSCheckInputError("Enter a valid hostname.")
    try:
        hostname = raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise DNSCheckInputError("Enter a valid hostname.") from exc
    labels = hostname.split(".")
    if len(hostname) > 253 or any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        or not all(char.isascii() and (char.isalnum() or char == "-") for char in label)
        for label in labels
    ):
        raise DNSCheckInputError("Enter a valid hostname.")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise DNSCheckInputError("Enter a hostname, not an IP address.")
    return hostname


def _encode_query(hostname: str, record_type: str, query_id: int) -> bytes:
    qname = b"".join(bytes((len(label),)) + label.encode("ascii")
                    for label in hostname.split(".")) + b"\0"
    return struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + qname + struct.pack(
        "!HH", RECORD_TYPES[record_type], 1)


def _decode_name(packet: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    cursor = offset
    next_offset: int | None = None
    visited: set[int] = set()
    while True:
        if cursor >= len(packet):
            raise ValueError("truncated DNS name")
        length = packet[cursor]
        if length & 0xC0 == 0xC0:
            if cursor + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | packet[cursor + 1]
            if pointer in visited or pointer >= len(packet):
                raise ValueError("invalid DNS pointer")
            visited.add(pointer)
            if next_offset is None:
                next_offset = cursor + 2
            cursor = pointer
            continue
        if length & 0xC0:
            raise ValueError("invalid DNS label")
        cursor += 1
        if length == 0:
            return ".".join(labels), next_offset if next_offset is not None else cursor
        if length > 63 or cursor + length > len(packet):
            raise ValueError("truncated DNS label")
        labels.append(packet[cursor:cursor + length].decode("ascii"))
        cursor += length


def _skip_name(packet: bytes, offset: int) -> int:
    _name, next_offset = _decode_name(packet, offset)
    return next_offset


def _parse_answers(packet: bytes, expected_id: int, record_type: str) -> tuple[int, list[str]]:
    if len(packet) < 12:
        raise ValueError("truncated DNS response")
    query_id, flags, questions, answers, _authorities, _additional = struct.unpack(
        "!HHHHHH", packet[:12])
    if query_id != expected_id or not flags & 0x8000:
        raise ValueError("invalid DNS response")
    rcode = flags & 0xF
    offset = 12
    for _ in range(questions):
        offset = _skip_name(packet, offset)
        if offset + 4 > len(packet):
            raise ValueError("truncated DNS question")
        offset += 4
    values: list[str] = []
    expected_type = RECORD_TYPES[record_type]
    for _ in range(answers):
        offset = _skip_name(packet, offset)
        if offset + 10 > len(packet):
            raise ValueError("truncated DNS answer")
        rr_type, rr_class, _ttl, data_length = struct.unpack("!HHIH", packet[offset:offset + 10])
        offset += 10
        end = offset + data_length
        if end > len(packet):
            raise ValueError("truncated DNS answer data")
        if rr_class == 1 and rr_type == expected_type:
            if record_type == "A" and data_length == 4:
                value = socket.inet_ntop(socket.AF_INET, packet[offset:end])
            elif record_type == "AAAA" and data_length == 16:
                value = socket.inet_ntop(socket.AF_INET6, packet[offset:end])
            elif record_type == "CNAME":
                value, _ = _decode_name(packet, offset)
            elif record_type == "MX" and data_length >= 3:
                priority = struct.unpack("!H", packet[offset:offset + 2])[0]
                exchange, _ = _decode_name(packet, offset + 2)
                value = f"{priority} {exchange}"
            elif record_type == "TXT":
                chunks: list[str] = []
                cursor = offset
                while cursor < end:
                    chunk_length = packet[cursor]
                    cursor += 1
                    if cursor + chunk_length > end:
                        raise ValueError("invalid TXT data")
                    chunks.append(packet[cursor:cursor + chunk_length].decode("utf-8", "replace"))
                    cursor += chunk_length
                value = "".join(chunks)
            else:
                value = ""
            if value:
                values.append(value)
        offset = end
    return rcode, values


_RCODE_STATUS = {0: "OK", 1: "Format error", 2: "Server failure", 3: "No records", 4: "Not supported", 5: "Refused"}


def _query_resolver(hostname: str, record_type: str, resolver: ResolverProfile) -> dict[str, Any]:
    query_id = secrets.randbits(16)
    query = _encode_query(hostname, record_type, query_id)
    started = time.perf_counter()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(RESOLVER_TIMEOUT_SECONDS)
            endpoint, port = resolver.endpoint.rsplit(":", 1)
            sock.connect((endpoint, int(port)))
            sock.send(query)
            response = sock.recv(4096)
        rcode, answers = _parse_answers(response, query_id, record_type)
        status = _RCODE_STATUS.get(rcode, f"DNS error {rcode}")
        if rcode == 0 and answers:
            status = "OK"
    except socket.timeout:
        answers, status = [], "Timeout"
    except (OSError, ValueError, struct.error) as exc:
        answers, status = [], "Network error" if isinstance(exc, OSError) else "Invalid DNS response"
    elapsed = (time.perf_counter() - started) * 1000
    return {"name": resolver.name, "region": resolver.region, "answers": answers,
            "response_ms": round(elapsed, 1), "status": status,
            "status_class": "ok" if status == "OK" else "error" if status in {"Timeout", "Network error", "Invalid DNS response"} else "warning"}


def check_dns(value: str, record_type: str = "A") -> dict[str, Any]:
    hostname = normalize_hostname(value)
    normalized_type = record_type.strip().upper() if isinstance(record_type, str) else ""
    if normalized_type not in RECORD_TYPES:
        raise DNSCheckInputError("Choose a supported record type.")
    with ThreadPoolExecutor(max_workers=min(MAX_RESOLVER_CONCURRENCY, len(RESOLVER_PROFILES))) as pool:
        rows = list(pool.map(
            lambda resolver: _query_resolver(hostname, normalized_type, resolver), RESOLVER_PROFILES))
    unique_answers = sorted({answer for row in rows for answer in row["answers"]}, key=str.casefold)
    fastest = min((row for row in rows if row["status"] == "OK"),
                  key=lambda row: row["response_ms"], default=None)
    for row in rows:
        row["fastest"] = row is fastest
    successful = sum(row["status"] == "OK" for row in rows)
    return {"hostname": hostname, "record_type": normalized_type, "rows": rows,
            "unique_answers": unique_answers, "unique_count": len(unique_answers),
            "resolver_count": len(rows), "successful_count": successful,
            "failed_count": len(rows) - successful,
            "overall_class": "error" if successful == 0 else "warning" if successful < len(rows) else "ok"}
