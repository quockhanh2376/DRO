from __future__ import annotations

import struct

import pytest

from app.core import dns_checker


def test_hostname_validation_normalizes_and_rejects_unsafe_input():
    assert dns_checker.normalize_hostname("  ExAmPlE.com. ") == "example.com"
    with pytest.raises(dns_checker.DNSCheckInputError):
        dns_checker.normalize_hostname("https://example.com/path")
    with pytest.raises(dns_checker.DNSCheckInputError):
        dns_checker.normalize_hostname("bad..example.com")
    with pytest.raises(dns_checker.DNSCheckInputError):
        dns_checker.normalize_hostname("192.0.2.1")


def test_valid_record_type_is_used_and_answers_are_deduplicated(monkeypatch):
    calls = []

    def fake_query(hostname, record_type, resolver):
        calls.append((hostname, record_type, resolver.name))
        answer = "2001:db8::1" if record_type == "AAAA" else "192.0.2.5"
        return {"name": resolver.name, "region": resolver.region, "answers": [answer],
                "response_ms": 8.0, "status": "OK", "status_class": "ok"}

    monkeypatch.setattr(dns_checker, "_query_resolver", fake_query)
    result = dns_checker.check_dns("Example.com.", "aaaa")
    assert result["hostname"] == "example.com"
    assert result["record_type"] == "AAAA"
    assert len(calls) == 10
    assert {call[1] for call in calls} == {"AAAA"}
    assert result["unique_answers"] == ["2001:db8::1"]
    assert result["unique_count"] == 1
    assert result["resolver_count"] == 10
    assert result["successful_count"] == 10


def test_timeout_from_one_resolver_does_not_fail_entire_check(monkeypatch):
    def fake_query(hostname, record_type, resolver):
        if resolver.name == "VN Resolver":
            return {"name": resolver.name, "region": resolver.region, "answers": [], "response_ms": 1.0,
                    "status": "Timeout", "status_class": "error"}
        return {"name": resolver.name, "region": resolver.region,
                "answers": ["192.0.2.8"], "response_ms": float(20 - len(resolver.name)),
                "status": "OK", "status_class": "ok"}

    monkeypatch.setattr(dns_checker, "_query_resolver", fake_query)
    result = dns_checker.check_dns("example.com", "A")
    assert result["successful_count"] == 9
    assert result["failed_count"] == 1
    assert result["overall_class"] == "warning"
    assert result["rows"][4]["status"] == "Timeout"
    assert not result["rows"][4]["fastest"]
    assert result["rows"][5]["fastest"]
    assert result["unique_count"] == 1


def test_dns_a_response_parser_decodes_a_record():
    query_id = 0x1234
    question = b"\x07example\x03com\0" + struct.pack("!HH", 1, 1)
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + bytes((192, 0, 2, 9))
    packet = struct.pack("!HHHHHH", query_id, 0x8180, 1, 1, 0, 0) + question + answer
    assert dns_checker._parse_answers(packet, query_id, "A") == (0, ["192.0.2.9"])


def test_supported_resolvers_and_worker_limit_are_small_and_explicit():
    profiles = dns_checker.RESOLVER_PROFILES
    assert [profile.name for profile in profiles[:4]] == ["Google", "Cloudflare", "Quad9", "OpenDNS"]
    assert [profile.region for profile in profiles[:4]] == ["Global / Anycast"] * 4
    assert [profile.endpoint for profile in profiles[:4]] == [
        "8.8.8.8:53", "1.1.1.1:53", "9.9.9.9:53", "208.67.222.222:53"]
    assert [(profile.name, profile.region, profile.endpoint) for profile in profiles[4:]] == [
        ("VN Resolver", "Vietnam", "42.116.255.180:53"),
        ("SG Resolver", "Singapore", "129.126.119.238:53"),
        ("MY Resolver", "Malaysia", "1.9.63.98:53"),
        ("HK Resolver", "Hong Kong", "218.255.10.58:53"),
        ("AU Resolver", "Australia", "203.54.212.126:53"),
        ("JP Resolver", "Japan", "219.163.11.226:53"),
    ]
    assert all(profile.protocol == "UDP DNS" for profile in profiles)
    assert dns_checker.MAX_RESOLVER_CONCURRENCY <= 4
    assert set(dns_checker.RECORD_TYPES) == {"A", "AAAA", "CNAME", "MX", "TXT"}


def test_fastest_is_the_lowest_latency_successful_resolver_only(monkeypatch):
    def fake_query(hostname, record_type, resolver):
        is_timeout = resolver.name == "Google"
        latency = 1.0 if is_timeout else {
            "Cloudflare": 23.0, "Quad9": 18.0, "OpenDNS": 38.0,
            "VN Resolver": 41.0, "SG Resolver": 32.0, "MY Resolver": 27.0,
            "HK Resolver": 22.0, "AU Resolver": 210.0, "JP Resolver": 99.0,
        }[resolver.name]
        return {"name": resolver.name, "region": resolver.region, "answers": [],
                "response_ms": latency, "status": "Timeout" if is_timeout else "OK",
                "status_class": "error" if is_timeout else "ok"}

    monkeypatch.setattr(dns_checker, "_query_resolver", fake_query)
    rows = dns_checker.check_dns("example.com")["rows"]
    fastest = [row for row in rows if row["fastest"]]
    assert len(fastest) == 1
    assert fastest[0]["name"] == "Quad9"
    assert fastest[0]["response_ms"] == 18.0
