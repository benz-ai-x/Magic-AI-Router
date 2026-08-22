"""Tests for suanpan/router.py — routing decisions."""
import unittest

from suanpan.config import AppConfig, ProviderConfig, RouterConfig
from suanpan.router import decide_route, NoRouteMatched


def _config(providers=None, router=None, rules=None):
    """Build a minimal AppConfig for testing."""
    return AppConfig(
        providers=providers or {"p": ProviderConfig(base_url="http://x")},
        router=router or RouterConfig(default="p/model-a"),
        rules=rules or [],
    )


class TestInlineOverride(unittest.TestCase):
    def test_slash_in_model_routes_directly(self):
        cfg = _config()
        body = {"model": "p/model-b"}
        d = decide_route(body, config=cfg)
        self.assertEqual(d.provider, "p")
        self.assertEqual(d.target_model, "model-b")
        self.assertEqual(d.scenario, "inline")

    def test_comma_in_model_routes_directly(self):
        cfg = _config()
        body = {"model": "p,model-c"}
        d = decide_route(body, config=cfg)
        self.assertEqual(d.provider, "p")
        self.assertEqual(d.target_model, "model-c")

    def test_unknown_provider_falls_through(self):
        cfg = _config()
        body = {"model": "unknown/model"}
        d = decide_route(body, config=cfg)
        self.assertEqual(d.scenario, "default")




class TestFallbackObservable(unittest.TestCase):
    """#50：显式路由意图（内联/SUBAGENT）遇未知或停用 provider 时仍
    fall-through（容错刻意），但决策必须携带原意图——响应头/日志可
    感知，绝不静默误投。"""

    def test_inline_unknown_provider_marks_fallback(self):
        cfg = _config()
        d = decide_route({"model": "ghost/model-x"}, config=cfg)
        self.assertEqual(d.scenario, "default")
        self.assertEqual(d.fallback_from, "ghost/model-x")

    def test_inline_disabled_provider_marks_fallback(self):
        cfg = _config(
            providers={"p": ProviderConfig(base_url="http://x", enabled=False),
                       "q": ProviderConfig(base_url="http://y")},
            router=RouterConfig(default="q/model-a"))
        d = decide_route({"model": "p/model-x"}, config=cfg)
        self.assertEqual(d.scenario, "default")
        self.assertEqual(d.fallback_from, "p/model-x")

    def test_subagent_disabled_provider_marks_fallback(self):
        cfg = _config(
            providers={"p": ProviderConfig(base_url="http://x", enabled=False),
                       "q": ProviderConfig(base_url="http://y")},
            router=RouterConfig(default="q/model-a"))
        body = {"model": "x", "system": "<SUBAGENT-MODEL>p/model-b</SUBAGENT-MODEL>"}
        d = decide_route(body, config=cfg)
        self.assertEqual(d.fallback_from, "p/model-b")


    def test_fallback_intent_sanitized_no_control_chars(self):
        """#50 复核：model 来自客户端请求体——CR/LF/控制字符不得进入
        fallback_from（否则响应头构造在误投之后 500，可感知性反被摧毁）。"""
        cfg = _config()
        d = decide_route({"model": "ghost/x\r\nSet-Cookie: evil=1"},
                         config=cfg)
        self.assertNotIn("\r", d.fallback_from)
        self.assertNotIn("\n", d.fallback_from)
        self.assertTrue(d.fallback_from.startswith("ghost/x"))

    def test_normal_routes_carry_no_fallback(self):
        cfg = _config()
        self.assertIsNone(decide_route({"model": "p/m"}, config=cfg).fallback_from)
        self.assertIsNone(decide_route({"model": "x"}, config=cfg).fallback_from)


class TestSubagentModel(unittest.TestCase):
    def test_subagent_tag_routes(self):
        cfg = _config()
        body = {"model": "x", "system": "<SUBAGENT-MODEL>p/model-b</SUBAGENT-MODEL>"}
        d = decide_route(body, config=cfg)
        self.assertEqual(d.provider, "p")
        self.assertEqual(d.target_model, "model-b")
        self.assertEqual(d.scenario, "subagent")
        self.assertTrue(d.strip_marker)

    def test_disabled_provider_in_subagent_falls_through(self):
        cfg = _config(
            providers={"p": ProviderConfig(base_url="http://x", enabled=False),
                       "q": ProviderConfig(base_url="http://y")},
            router=RouterConfig(default="q/model-a"))
        body = {"model": "x", "system": "<SUBAGENT-MODEL>p/model-b</SUBAGENT-MODEL>"}
        d = decide_route(body, config=cfg)
        self.assertNotEqual(d.scenario, "subagent")


class TestRules(unittest.TestCase):
    def test_prefix_rule_matches(self):
        from suanpan.config import Rule
        cfg = _config(rules=[Rule(match_prefix="claude-sonnet", route_to="p/flash")])
        body = {"model": "claude-sonnet-4-20250514"}
        d = decide_route(body, config=cfg)
        self.assertEqual(d.scenario, "rule")
        self.assertEqual(d.target_model, "flash")

    def test_non_matching_prefix_falls_through(self):
        from suanpan.config import Rule
        cfg = _config(rules=[Rule(match_prefix="gpt", route_to="p/gpt")])
        body = {"model": "claude-sonnet"}
        d = decide_route(body, config=cfg)
        self.assertEqual(d.scenario, "default")

    def test_multiple_rules_first_match_wins(self):
        from suanpan.config import Rule
        cfg = _config(rules=[
            Rule(match_prefix="claude", route_to="p/first"),
            Rule(match_prefix="claude-sonnet", route_to="p/second"),
        ])
        body = {"model": "claude-sonnet-4"}
        d = decide_route(body, config=cfg)
        self.assertEqual(d.target_model, "first")


class TestDefaultFallback(unittest.TestCase):
    def test_no_match_uses_default(self):
        cfg = _config()
        body = {"model": "unknown-model"}
        d = decide_route(body, config=cfg)
        self.assertEqual(d.scenario, "default")

    def test_no_route_matched_raises(self):
        cfg = _config(router=RouterConfig())
        body = {"model": "x"}
        with self.assertRaises(NoRouteMatched):
            decide_route(body, config=cfg)


if __name__ == "__main__":
    unittest.main()
