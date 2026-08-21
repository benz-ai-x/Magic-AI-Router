"""Tests for provider_auth — 供应商认证的唯一纯逻辑实现。

Seam: two pure functions shared by suanpan/config.py (typed ProviderConfig
methods delegate) and balance_usage.py (dict path, no pydantic needed).
"""
import os
import unittest

from mpconf.provider_auth import build_outbound_headers, resolve_api_key


class TestResolveApiKey(unittest.TestCase):
    def test_literal_key_wins(self):
        self.assertEqual(resolve_api_key({"api_key": "sk-x"}), "sk-x")

    def test_none_key_falls_back_to_env(self):
        # The masked UI view sends api_key=None (+ api_key_set), never a
        # placeholder string — so a None key falls through to the env var.
        os.environ["TEST_PA_KEY"] = "sk-from-env"
        try:
            p = {"api_key": None, "api_key_env": "TEST_PA_KEY"}
            self.assertEqual(resolve_api_key(p), "sk-from-env")
        finally:
            del os.environ["TEST_PA_KEY"]

    def test_no_key_no_env_returns_none(self):
        self.assertIsNone(resolve_api_key({"api_key": None}))

    def test_missing_env_var_returns_none(self):
        self.assertIsNone(resolve_api_key({"api_key_env": "DEFINITELY_UNSET_VAR"}))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(resolve_api_key({}))


class TestBuildOutboundHeaders(unittest.TestCase):
    def test_x_api_key_style(self):
        h = build_outbound_headers({}, "sk-x", auth_header="x-api-key")
        self.assertEqual(h, {"x-api-key": "sk-x"})

    def test_authorization_style(self):
        h = build_outbound_headers({}, "sk-x", auth_header="Authorization")
        self.assertEqual(h, {"Authorization": "Bearer sk-x"})

    def test_default_style_is_authorization(self):
        h = build_outbound_headers({}, "sk-x")
        self.assertEqual(h, {"Authorization": "Bearer sk-x"})

    def test_hop_headers_filtered(self):
        incoming = {"Host": "x", "Content-Length": "5", "Connection": "keep-alive",
                    "Authorization": "Bearer old", "X-Api-Key": "old",
                    "anthropic-version": "2023-06-01"}
        h = build_outbound_headers(incoming, "sk-new", auth_header="x-api-key")
        self.assertEqual(h, {"anthropic-version": "2023-06-01", "x-api-key": "sk-new"})

    def test_no_key_passes_through_incoming_auth(self):
        """issue #9 契约反转：keyless 出站剥除一切入站凭证。"""
        incoming = {"authorization": "Bearer oauth-token", "x-api-key": "k"}
        h = build_outbound_headers(incoming, None)
        self.assertNotIn("authorization", h)
        self.assertNotIn("x-api-key", h)

    def test_no_key_no_incoming_auth(self):
        self.assertEqual(build_outbound_headers({}, None), {})

    def test_unknown_auth_header_defaults_to_bearer(self):
        # auth_header=None with a key: Bearer default (balance_usage convention)
        self.assertEqual(build_outbound_headers({}, "sk-x", auth_header=None),
                         {"Authorization": "Bearer sk-x"})

    def test_auth_header_case_insensitive_x_api_key(self):
        """"X-API-Key" (any casing) must select x-api-key, not silently fall
        back to Bearer."""
        for variant in ("X-API-Key", "X-Api-Key", "X-API-KEY"):
            h = build_outbound_headers({}, "sk-x", auth_header=variant)
            self.assertEqual(h, {"x-api-key": "sk-x"}, f"failed for {variant!r}")

    def test_auth_header_case_insensitive_authorization(self):
        """"authorization" (lowercase) must still produce Bearer."""
        h = build_outbound_headers({}, "sk-x", auth_header="authorization")
        self.assertEqual(h, {"Authorization": "Bearer sk-x"})


class TestGatewayKeyPassthrough(unittest.TestCase):
    """Without a provider key, incoming auth is passed through for OAuth
    backends — but never the gateway's own gate key (#1: a client's
    x-api-key/Authorization carrying the gateway key must not leak to a
    third-party backend)."""

    def test_gateway_key_x_api_key_not_passed_through(self):
        h = build_outbound_headers({"x-api-key": "gate-key"}, None,
                                   gateway_key="gate-key")
        self.assertEqual(h, {})

    def test_gateway_key_bearer_not_passed_through(self):
        h = build_outbound_headers({"authorization": "Bearer gate-key"}, None,
                                   gateway_key="gate-key")
        self.assertEqual(h, {})

    def test_other_auth_still_passed_through(self):
        """issue #9 契约反转：keyless 出站剥除一切入站凭证。"""
        incoming = {"authorization": "Bearer oauth-token", "x-api-key": "k"}
        h = build_outbound_headers(incoming, None, gateway_key="gate-key")
        self.assertNotIn("authorization", h)
        self.assertNotIn("x-api-key", h)

    def test_gateway_key_moot_when_provider_key_set(self):
        # With a provider key, outbound auth is provider's own; the gateway
        # key parameter must not change anything.
        h = build_outbound_headers({"x-api-key": "gate-key"}, "sk-x",
                                   auth_header="x-api-key",
                                   gateway_key="gate-key")
        self.assertEqual(h, {"x-api-key": "sk-x"})

    def test_no_gateway_key_keeps_passthrough(self):
        """issue #9 契约反转：keyless 出站剥除一切入站凭证。"""
        incoming = {"authorization": "Bearer whatever", "x-api-key": "k"}
        h = build_outbound_headers(incoming, None)
        self.assertNotIn("authorization", h)
        self.assertNotIn("x-api-key", h)


if __name__ == "__main__":
    unittest.main()
