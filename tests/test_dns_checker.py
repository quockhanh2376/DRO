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
        calls.append((hostname, record_type, resolver["name"]))
        answer = "2001:db8::1" if record_type == "AAAA" else "192.0.2.5"
        return {"name": resolver["name"], "region": resolver["region"], "answers": [answer],
                "response_ms": 8.0, "status": "OK", "status_class": "ok"}

    monkeypatch.setattr(dns_checker, "_query_resolver", fake_query)
    result = dns_checker.check_dns("Example.com.", "aaaa")
    assert result["hostname"] == "example.com"
    assert result["record_type"] == "AAAA"
    assert len(calls) == 4
    assert {call[1] for call in calls} == {"AAAA"}
    assert result["unique_answers"] == ["2001:db8::1"]
    assert result["unique_count"] == 1
    assert result["resolver_count"] == 4
    assert result["successful_count"] == 4


def test_timeout_from_one_resolver_does_not_fail_entire_check(monkeypatch):
    def fake_query(hostname, record_type, resolver):
        if resolver["name"] == "Quad9":
            return {"name": "Quad9", "region": "Global", "answers": [], "response_ms": 3000.0,
                    "status": "Timeout", "status_class": "error"}
        return {"name": resolver["name"], "region": resolver["region"],
                "answers": ["192.0.2.8"], "response_ms": 12.0, "status": "OK", "status_class": "ok"}

    monkeypatch.setattr(dns_checker, "_query_resolver", fake_query)
    result = dns_checker.check_dns("example.com", "A")
    assert result["successful_count"] == 3
    assert result["failed_count"] == 1
    assert result["overall_class"] == "warning"
    assert result["rows"][2]["status"] == "Timeout"
    assert result["unique_count"] == 1


def test_dns_a_response_parser_decodes_a_record():
    query_id = 0x1234
    question = b"\x07example\x03com\0" + struct.pack("!HH", 1, 1)
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + bytes((192, 0, 2, 9))
    packet = struct.pack("!HHHHHH", query_id, 0x8180, 1, 1, 0, 0) + question + answer
    assert dns_checker._parse_answers(packet, query_id, "A") == (0, ["192.0.2.9"])


def test_supported_resolvers_and_worker_limit_are_small_and_explicit():
    assert [resolver["name"] for resolver in dns_checker.RESOLVERS] == [
        "Google", "Cloudflare", "Quad9", "OpenDNS"]
    assert dns_checker.MAX_RESOLVER_CONCURRENCY <= 4
    assert set(dns_checker.RECORD_TYPES) == {"A", "AAAA", "CNAME", "MX", "TXT"}
