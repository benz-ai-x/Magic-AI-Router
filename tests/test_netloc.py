"""Tests for netloc.py — host:port listen 地址的唯一解析/校验/构造者。

Seam: three pure functions. Every listen parse in the repo converges here
(proxy 本地代理 / Suanpan gateway 两条启动路径 / merge_config 校验 /
系统代理目标 / app 端口探测 / config_server 派生字段)。
"""
import unittest

from netloc import LOOPBACK_HOSTS, format_listen, parse_listen, require_loopback


class TestParseListen(unittest.TestCase):
    def test_plain_ipv4(self):
        self.assertEqual(parse_listen("127.0.0.1:9527"), ("127.0.0.1", 9527))

    def test_empty_host_normalizes_to_loopback(self):
        # 统一语义：":9527" 即 loopback（此前 suanpan_runtime 接受、config.py 拒绝）
        self.assertEqual(parse_listen(":9527"), ("127.0.0.1", 9527))

    def test_bracketed_ipv6(self):
        self.assertEqual(parse_listen("[::1]:9527"), ("::1", 9527))

    def test_bare_ipv6_without_port_rejected(self):
        # #40: "::1" used to rpartition into host "::" + port 1. A bare
        # IPv6 literal is host-only; a port requires brackets.
        with self.assertRaises(ValueError):
            parse_listen("::1")

    def test_bare_ipv6_with_port_requires_brackets(self):
        # "::1:9527" is itself a valid IPv6 address, so it cannot carry a
        # port unambiguously — the bracketed form is the only port syntax.
        with self.assertRaises(ValueError):
            parse_listen("::1:9527")

    def test_hostname(self):
        self.assertEqual(parse_listen("localhost:8888"), ("localhost", 8888))

    def test_whitespace_trimmed(self):
        self.assertEqual(parse_listen(" 127.0.0.1:9527 "), ("127.0.0.1", 9527))

    def test_missing_port_rejected(self):
        with self.assertRaises(ValueError):
            parse_listen("127.0.0.1")

    def test_non_digit_port_rejected(self):
        with self.assertRaises(ValueError):
            parse_listen("127.0.0.1:http")

    def test_port_zero_rejected(self):
        with self.assertRaises(ValueError):
            parse_listen("127.0.0.1:0")

    def test_port_above_max_rejected(self):
        with self.assertRaises(ValueError):
            parse_listen("127.0.0.1:99999")

    def test_port_boundaries_accepted(self):
        self.assertEqual(parse_listen("127.0.0.1:1"), ("127.0.0.1", 1))
        self.assertEqual(parse_listen("127.0.0.1:65535"), ("127.0.0.1", 65535))

    def test_non_string_rejected(self):
        with self.assertRaises(ValueError):
            parse_listen(None)
        with self.assertRaises(ValueError):
            parse_listen(9527)

    def test_default_port_fills_bare_host(self):
        self.assertEqual(parse_listen("127.0.0.1", default_port=9527), ("127.0.0.1", 9527))
        self.assertEqual(parse_listen("localhost", default_port=9528), ("localhost", 9528))

    def test_default_port_fills_bare_and_bracketed_ipv6(self):
        # #40: bare "::1" was misparsed as host "::" + port 1; "[::1]"
        # without a port raised instead of taking the default.
        self.assertEqual(parse_listen("::1", default_port=9527), ("::1", 9527))
        self.assertEqual(parse_listen("[::1]", default_port=9527), ("::1", 9527))

    def test_default_port_ignored_when_port_present(self):
        self.assertEqual(parse_listen("127.0.0.1:8888", default_port=9527), ("127.0.0.1", 8888))

    def test_default_port_still_rejects_bad_port(self):
        with self.assertRaises(ValueError):
            parse_listen("127.0.0.1:http", default_port=9527)


class TestRequireLoopback(unittest.TestCase):
    def test_loopback_hosts_accepted(self):
        for host in LOOPBACK_HOSTS:
            require_loopback(host)  # no raise

    def test_expected_set(self):
        self.assertEqual(LOOPBACK_HOSTS, frozenset({"127.0.0.1", "::1", "localhost"}))

    def test_wildcard_rejected(self):
        with self.assertRaises(ValueError):
            require_loopback("0.0.0.0")

    def test_public_ip_rejected(self):
        with self.assertRaises(ValueError):
            require_loopback("203.0.113.10")


class TestFormatListen(unittest.TestCase):
    def test_ipv4(self):
        self.assertEqual(format_listen("127.0.0.1", 9527), "127.0.0.1:9527")

    def test_ipv6_bracketed(self):
        self.assertEqual(format_listen("::1", 9527), "[::1]:9527")

    def test_round_trip(self):
        for text in ("127.0.0.1:9527", "[::1]:8080", "localhost:1080"):
            host, port = parse_listen(text)
            self.assertEqual(format_listen(host, port), text)


if __name__ == "__main__":
    unittest.main()
